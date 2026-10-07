using System.IO;
using System.Runtime.InteropServices;
using AndroidPhotoTransfer.Infrastructure;
using MediaDevices;

namespace AndroidPhotoTransfer.Core.Wpd
{
    /// <summary>Lists portable devices that look like phones/cameras.</summary>
    internal static class WpdDeviceList
    {
        /// <summary>Must run on the <see cref="MtpWorker"/>.</summary>
        public static IReadOnlyList<PhoneListing> GetPhones(bool includeStorageDevices)
        {
            var phones = new List<PhoneListing>();
            foreach (var device in MediaDevice.GetDevices())
            {
                try
                {
                    var id = device.DeviceId;
                    if (!includeStorageDevices && IsMassStorageVolume(id)) continue;
                    phones.Add(new PhoneListing(id, DisplayName(device)));
                }
                catch (Exception ex)
                {
                    Logger.Warn("Skipped a portable device that could not be read", ex);
                }
                finally
                {
                    try { device.Dispose(); } catch { /* not connected; nothing to release */ }
                }
            }
            return phones;
        }

        /// <summary>
        /// USB sticks and external drives also appear as WPD devices (through the WPD bus enumerator)
        /// but are ordinary drives in Explorer, so they are not offered as phones.
        /// </summary>
        internal static bool IsMassStorageVolume(string deviceId) =>
            deviceId.Contains("wpdbusenum", StringComparison.OrdinalIgnoreCase) ||
            deviceId.Contains("usbstor", StringComparison.OrdinalIgnoreCase);

        internal static string DisplayName(MediaDevice device)
        {
            foreach (var candidate in new Func<string?>[] { () => device.FriendlyName, () => device.Description, () => device.Model })
            {
                try
                {
                    var name = candidate()?.Trim();
                    if (!string.IsNullOrEmpty(name)) return name;
                }
                catch
                {
                    // Some properties are unavailable on some devices.
                }
            }
            return "Android device";
        }
    }

    /// <summary><see cref="IWpdAdapter"/> backed by the MediaDevices library. All calls are marshalled to the <see cref="MtpWorker"/>.</summary>
    internal sealed class WpdAdapter : IWpdAdapter
    {
        private const int ListBatchSize = 200;
        private readonly MtpWorker _worker;
        private readonly MediaDevice _device;
        private int _disposed;
        private volatile bool _connectionLost;

        private WpdAdapter(MtpWorker worker, MediaDevice device, PhoneInfo info)
        {
            _worker = worker;
            _device = device;
            Info = info;
        }

        public PhoneInfo Info { get; }

        public bool IsConnected
        {
            get
            {
                if (_connectionLost || Volatile.Read(ref _disposed) == 1) return false;
                try { return _device.IsConnected; } catch { return false; }
            }
        }

        public static Task<WpdAdapter> ConnectAsync(MtpWorker worker, string deviceId, CancellationToken ct) =>
            worker.RunAsync(() =>
            {
                MediaDevice? match = null;
                foreach (var candidate in MediaDevice.GetDevices())
                {
                    if (match == null && candidate.DeviceId == deviceId) match = candidate;
                    else candidate.Dispose();
                }
                if (match == null) throw new DeviceDisconnectedException();

                try
                {
                    match.Connect();
                    var name = WpdDeviceList.DisplayName(match);
                    return new WpdAdapter(worker, match, new PhoneInfo(deviceId, name, StableKey(match, deviceId)));
                }
                catch
                {
                    match.Dispose();
                    throw;
                }
            }, ct);

        private static string StableKey(MediaDevice device, string deviceId)
        {
            try
            {
                var serial = device.SerialNumber?.Trim();
                if (!string.IsNullOrEmpty(serial)) return "SN:" + serial;
            }
            catch
            {
                // Fall back to the device id below.
            }
            return "ID:" + deviceId;
        }

        /// <summary>
        /// Runs a device call and turns "the connection is gone" errors into <see cref="DeviceDisconnectedException"/>,
        /// remembering that this connection is dead so it gets replaced instead of being retried file after file.
        /// </summary>
        private T Guard<T>(Func<T> call)
        {
            if (_connectionLost) throw new DeviceDisconnectedException();
            try
            {
                return call();
            }
            catch (Exception ex) when (IsConnectionLoss(ex))
            {
                if (!_connectionLost) Logger.Warn($"Connection to {Info.Name} was lost", ex);
                _connectionLost = true;
                throw new DeviceDisconnectedException(inner: ex);
            }
        }

        private static bool IsConnectionLoss(Exception ex) =>
            ex is DeviceDisconnectedException ||
            ex.GetType().Name == "NotConnectedException" ||
            ex is COMException { HResult: unchecked((int)0x8007048F) /* device not connected */
                or unchecked((int)0x800701B1) /* no such device */
                or unchecked((int)0x80070037) /* device no longer exists */ };

        public Task<IReadOnlyList<StorageRoot>> GetStorageRootsAsync(CancellationToken ct) =>
            _worker.RunAsync<IReadOnlyList<StorageRoot>>(() => Guard(() =>
            {
                var roots = new List<StorageRoot>();
                foreach (var drive in _device.GetDrives() ?? Array.Empty<MediaDriveInfo>())
                {
                    try
                    {
                        var root = drive.RootDirectory;
                        var name = Try(() => drive.VolumeLabel);
                        if (string.IsNullOrWhiteSpace(name)) name = root.Name;
                        if (string.IsNullOrWhiteSpace(name)) name = drive.Name;
                        roots.Add(new StorageRoot(root.FullName, name!.Trim().TrimStart('\\'),
                            Try(() => drive.TotalSize), Try(() => drive.AvailableFreeSpace)));
                    }
                    catch (Exception ex) when (!IsConnectionLoss(ex))
                    {
                        Logger.Warn("Skipped unreadable phone storage", ex);
                    }
                }
                return roots;
            }), ct);

        public Task<bool> FolderHasEntriesAsync(string path, CancellationToken ct) =>
            _worker.RunAsync(() => Guard(() => _device.GetDirectoryInfo(path).EnumerateFileSystemInfos().Any()), ct);

        public async Task ListFolderAsync(string path, Action<IReadOnlyList<DeviceEntry>> onBatch, CancellationToken ct)
        {
            var enumerator = await _worker.RunAsync(
                () => Guard(() => _device.GetDirectoryInfo(path).EnumerateFileSystemInfos().GetEnumerator()), ct);
            try
            {
                while (true)
                {
                    var (batch, done) = await _worker.RunAsync(() => Guard(() =>
                    {
                        var list = new List<DeviceEntry>(ListBatchSize);
                        while (list.Count < ListBatchSize)
                        {
                            if (!enumerator.MoveNext()) return (list, true);
                            var entry = ToEntry(enumerator.Current);
                            if (entry != null) list.Add(entry);
                        }
                        return (list, false);
                    }), ct);

                    if (batch.Count > 0) onBatch(batch);
                    if (done) return;
                }
            }
            finally
            {
                try { _ = _worker.RunAsync(enumerator.Dispose); } catch (ObjectDisposedException) { }
            }
        }

        private static DeviceEntry? ToEntry(MediaFileSystemInfo info)
        {
            try
            {
                var attributes = info.Attributes;
                bool isFolder = info is MediaDirectoryInfo || attributes.HasFlag(MediaFileAttributes.Directory);
                bool isHidden = attributes.HasFlag(MediaFileAttributes.Hidden) || attributes.HasFlag(MediaFileAttributes.System);
                long size = isFolder ? 0 : (long)Math.Min(info.Length, long.MaxValue);
                string? persistentId = isFolder ? null : Try(() => info.PersistentUniqueId);
                return new DeviceEntry(info.FullName, info.Name, isFolder, isHidden, size, info.CreationTime, info.LastWriteTime,
                    string.IsNullOrEmpty(persistentId) ? null : persistentId);
            }
            catch (Exception ex) when (!IsConnectionLoss(ex))
            {
                Logger.Warn("Skipped an unreadable phone object", ex);
                return null;
            }
        }

        /// <summary>
        /// Finds a file by its permanent ID (one quick request) and falls back to the path, which the phone
        /// resolves by listing every folder on the way — slow in big folders like DCIM/Camera.
        /// </summary>
        private MediaFileInfo ResolveFile(string path, string? persistentId)
        {
            if (!string.IsNullOrEmpty(persistentId))
            {
                try
                {
                    if (_device.GetFileSystemInfoFromPersistentUniqueId(persistentId) is MediaFileInfo file) return file;
                }
                catch (Exception ex) when (!IsConnectionLoss(ex))
                {
                    // Unknown ID (e.g. the phone renumbered its files): use the path instead.
                }
            }
            return _device.GetFileInfo(path);
        }

        public async Task<Stream> OpenReadAsync(string path, string? persistentId, CancellationToken ct)
        {
            var inner = await _worker.RunAsync(() => Guard(() => ResolveFile(path, persistentId).OpenRead()), ct, highPriority: true);
            return new WorkerStream(inner, this);
        }

        public Task<byte[]?> GetThumbnailAsync(string path, string? persistentId, CancellationToken ct) =>
            _worker.RunAsync<byte[]?>(() => Guard(() =>
            {
                try
                {
                    using var thumbnail = ResolveFile(path, persistentId).OpenThumbnail();
                    using var buffer = new MemoryStream();
                    thumbnail.CopyTo(buffer);
                    return buffer.Length > 0 ? buffer.ToArray() : null;
                }
                catch (Exception ex) when (!IsConnectionLoss(ex))
                {
                    return null; // Many objects (e.g. some videos) simply have no thumbnail.
                }
            }), ct, highPriority: true);

        public Task<byte[]> ReadAllBytesAsync(string path, string? persistentId, long maxBytes, CancellationToken ct) =>
            _worker.RunAsync(() => Guard(() =>
            {
                var file = ResolveFile(path, persistentId);
                if ((long)file.Length > maxBytes) throw new InvalidOperationException("File is too large to load for preview.");
                using var source = file.OpenRead();
                using var buffer = new MemoryStream((int)file.Length);
                source.CopyTo(buffer);
                return buffer.ToArray();
            }), ct, highPriority: true);

        public async Task<bool> IsStillConnectedAsync()
        {
            if (!IsConnected) return false;
            try
            {
                var phones = await _worker.RunAsync(() => WpdDeviceList.GetPhones(includeStorageDevices: true), highPriority: true);
                return phones.Any(p => p.DeviceId == Info.DeviceId);
            }
            catch
            {
                return false;
            }
        }

        public void Dispose()
        {
            if (Interlocked.Exchange(ref _disposed, 1) == 1) return;
            try
            {
                _ = _worker.RunAsync(() =>
                {
                    try { if (_device.IsConnected) _device.Disconnect(); } catch { /* device already gone */ }
                    try { _device.Dispose(); } catch { /* device already gone */ }
                });
            }
            catch (ObjectDisposedException)
            {
                // App is shutting down; the worker thread is gone.
            }
        }

        private static T? Try<T>(Func<T> read)
        {
            try { return read(); } catch { return default; }
        }

        /// <summary>Read-only stream that performs each read of the underlying WPD stream on the MTP worker.</summary>
        private sealed class WorkerStream : Stream
        {
            private readonly Stream _inner;
            private readonly WpdAdapter _owner;

            public WorkerStream(Stream inner, WpdAdapter owner)
            {
                _inner = inner;
                _owner = owner;
            }

            public override bool CanRead => true;
            public override bool CanSeek => false;
            public override bool CanWrite => false;
            public override long Length => throw new NotSupportedException();
            public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }

            public override int Read(byte[] buffer, int offset, int count) =>
                _owner._worker.Run(() => _owner.Guard(() => _inner.Read(buffer, offset, count)));

            // Transfer reads jump ahead of background scanning; thumbnails are paused during transfers.
            public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) =>
                _owner._worker.RunAsync(() => _owner.Guard(() => _inner.Read(buffer, offset, count)), cancellationToken, highPriority: true);

            public override void Flush() { }
            public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
            public override void SetLength(long value) => throw new NotSupportedException();
            public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();

            protected override void Dispose(bool disposing)
            {
                if (disposing)
                {
                    try { _ = _owner._worker.RunAsync(_inner.Dispose); } catch (ObjectDisposedException) { }
                }
                base.Dispose(disposing);
            }
        }
    }
}
