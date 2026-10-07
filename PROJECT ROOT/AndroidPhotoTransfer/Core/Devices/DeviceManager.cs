using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Infrastructure;

namespace AndroidPhotoTransfer.Core.Devices
{
    /// <summary>
    /// Tracks which phone is connected. Refreshes are driven by Windows device-change notifications
    /// (see MainWindow), not polling. The only timer is a retry while a phone is locked or erroring.
    /// </summary>
    internal sealed class DeviceManager : IDeviceManager
    {
        private static readonly TimeSpan RetryInterval = TimeSpan.FromSeconds(3);
        private static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(30);

        private readonly MtpWorker _worker;
        private readonly bool _includeStorageDevices;
        private readonly SemaphoreSlim _gate = new(1, 1);
        private readonly Timer _retryTimer;
        private volatile string? _preferredDeviceId;
        private volatile bool _disposed;

        private volatile IWpdAdapter? _current;
        private volatile IReadOnlyList<PhoneListing> _phones = Array.Empty<PhoneListing>();
        private volatile IReadOnlyList<StorageRoot> _storageRoots = Array.Empty<StorageRoot>();
        private volatile string? _errorMessage;
        private volatile ConnectionState _state = ConnectionState.Waiting;

        private readonly string? _demoFolder;

        public DeviceManager(MtpWorker worker, bool includeStorageDevices, string? demoFolder = null)
        {
            _worker = worker;
            _includeStorageDevices = includeStorageDevices;
            _demoFolder = string.IsNullOrWhiteSpace(demoFolder) ? null : demoFolder;
            _retryTimer = new Timer(_ => _ = RefreshAsync(), null, Timeout.Infinite, Timeout.Infinite);
        }

        public ConnectionState State => _state;
        public IWpdAdapter? Current => _current;
        public IReadOnlyList<PhoneListing> Phones => _phones;
        public IReadOnlyList<StorageRoot> StorageRoots => _storageRoots;
        public string? ErrorMessage => _errorMessage;

        public event EventHandler? Changed;

        public async Task RefreshAsync()
        {
            if (_disposed) return;
            await _gate.WaitAsync();
            try
            {
                if (_disposed) return;

                var phones = await _worker.RunAsync(() => WpdDeviceList.GetPhones(_includeStorageDevices));
                if (_demoFolder != null && System.IO.Directory.Exists(_demoFolder))
                {
                    phones = phones.Append(new PhoneListing(LocalFolderAdapter.DeviceIdPrefix + System.IO.Path.GetFullPath(_demoFolder),
                        "Demo Phone (folder)")).ToList();
                }
                _phones = phones;

                var current = _current;
                if (current != null && phones.All(p => p.DeviceId != current.Info.DeviceId))
                {
                    Logger.Info($"Device disconnected: {current.Info.Name}");
                    DropCurrent();
                    current = null;
                }
                else if (current != null && !current.IsConnected)
                {
                    // Windows still lists the phone but this connection died (USB glitch, phone reset the session):
                    // replace it with a fresh connection instead of failing every request.
                    Logger.Info($"Connection to {current.Info.Name} is no longer usable; reconnecting");
                    DropCurrent();
                    current = null;
                }

                var target = current != null
                    ? phones.First(p => p.DeviceId == current.Info.DeviceId)
                    : phones.FirstOrDefault(p => p.DeviceId == _preferredDeviceId) ?? phones.FirstOrDefault();

                if (target == null)
                {
                    StopRetry();
                    SetState(ConnectionState.Waiting);
                    return;
                }

                if (current == null || _state is ConnectionState.Locked or ConnectionState.Error)
                {
                    await ConnectAsync(target);
                }
            }
            catch (ObjectDisposedException)
            {
                // Shutting down.
            }
            catch (Exception ex)
            {
                Logger.Error("Could not connect to the phone", ex);
                DropCurrent();
                _errorMessage = ErrorMessages.Describe(ex);
                SetState(ConnectionState.Error);
                ScheduleRetry();
            }
            finally
            {
                _gate.Release();
                RaiseChanged();
            }
        }

        public async Task ReconnectAsync()
        {
            if (_disposed) return;
            await _gate.WaitAsync();
            try
            {
                if (_current != null) Logger.Info($"Reconnecting to {_current.Info.Name} (user refresh)");
                DropCurrent();
            }
            finally
            {
                _gate.Release();
            }
            await RefreshAsync();
        }

        public async Task SelectPhoneAsync(string deviceId)
        {
            _preferredDeviceId = deviceId;
            if (_current?.Info.DeviceId == deviceId) return;

            await _gate.WaitAsync();
            try
            {
                DropCurrent();
            }
            finally
            {
                _gate.Release();
            }
            await RefreshAsync();
        }

        private async Task ConnectAsync(PhoneListing target)
        {
            var wasLocked = _state == ConnectionState.Locked;
            DropCurrent();
            if (!wasLocked)
            {
                SetState(ConnectionState.Connecting);
                RaiseChanged();
                Logger.Info($"Connecting to {target.Name}");
            }

            IWpdAdapter device = target.DeviceId.StartsWith(LocalFolderAdapter.DeviceIdPrefix, StringComparison.Ordinal)
                ? new LocalFolderAdapter(target.DeviceId[LocalFolderAdapter.DeviceIdPrefix.Length..])
                : await WpdAdapter.ConnectAsync(_worker, target.DeviceId, CancellationToken.None).WaitAsync(ConnectTimeout);
            _current = device;

            var roots = await device.GetStorageRootsAsync(CancellationToken.None).WaitAsync(ConnectTimeout);
            bool accessible = false;
            foreach (var root in roots)
            {
                try
                {
                    if (await device.FolderHasEntriesAsync(root.Path, CancellationToken.None).WaitAsync(ConnectTimeout))
                    {
                        accessible = true;
                        break;
                    }
                }
                catch (Exception ex) when (ex is not ObjectDisposedException)
                {
                    Logger.Warn($"Storage '{root.Name}' is not readable yet", ex);
                }
            }

            _storageRoots = roots;
            _errorMessage = null;

            if (accessible)
            {
                StopRetry();
                Logger.Info($"Device ready: {device.Info.Name} ({roots.Count} storage location(s))");
                SetState(ConnectionState.Ready);
            }
            else
            {
                if (!wasLocked) Logger.Info($"{device.Info.Name} is connected but its storage is unavailable (locked or not in File Transfer mode)");
                SetState(ConnectionState.Locked);
                ScheduleRetry();
            }
        }

        private void DropCurrent()
        {
            var current = _current;
            _current = null;
            _storageRoots = Array.Empty<StorageRoot>();
            current?.Dispose();
        }

        private void SetState(ConnectionState state) => _state = state;

        private void ScheduleRetry()
        {
            if (!_disposed) _retryTimer.Change(RetryInterval, Timeout.InfiniteTimeSpan);
        }

        private void StopRetry() => _retryTimer.Change(Timeout.Infinite, Timeout.Infinite);

        private void RaiseChanged()
        {
            if (!_disposed) Changed?.Invoke(this, EventArgs.Empty);
        }

        public void Dispose()
        {
            _disposed = true;
            _retryTimer.Dispose();
            DropCurrent();
        }
    }
}
