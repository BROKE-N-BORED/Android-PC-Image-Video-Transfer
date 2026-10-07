using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Windows.Threading;
using AndroidPhotoTransfer.Core.Devices;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Thumbnails;
using AndroidPhotoTransfer.Core.Transfer;
using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Data;
using AndroidPhotoTransfer.Infrastructure;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace AndroidPhotoTransfer.UI.ViewModels
{
    /// <summary>
    /// Owns the phone connection, the scan, the destination folder and the transfer panel.
    /// The two tabs (Photos & Videos, Files) are <see cref="LibraryViewModel"/>s fed from the same scan.
    /// </summary>
    public sealed partial class MainViewModel : ObservableObject
    {
        private static readonly TimeSpan FlushInterval = TimeSpan.FromMilliseconds(400);

        private readonly SettingsManager _settingsManager;
        private readonly IDeviceManager _devices;
        private readonly IMediaScanner _scanner;
        private readonly ThumbnailManager _thumbnails;
        private readonly IHistoryDatabase? _history;
        private readonly Dispatcher _dispatcher;

        private readonly ConcurrentQueue<IReadOnlyList<MediaItem>> _incoming = new();
        private readonly DispatcherTimer _flushTimer;
        private readonly DispatcherTimer _searchDebounce;
        private readonly DispatcherTimer _deviceChangeDebounce;
        private readonly HashSet<LibraryViewModel> _pendingSearch = new();
        private readonly Stopwatch _scanStopwatch = new();

        private IWpdAdapter? _activeDevice;
        private IWpdAdapter? _scannedDevice;
        private CancellationTokenSource? _scanCts;
        private HashSet<string> _transferredKeys = new(StringComparer.Ordinal);
        private DateTime _lastViewRefresh = DateTime.MinValue;
        private bool _syncingPhone;
        private string? _scanError;

        internal MainViewModel(SettingsManager settingsManager, IDeviceManager devices, IMediaScanner scanner,
            ThumbnailManager thumbnails, ITransferManager transferEngine, IHistoryDatabase? history, Dispatcher dispatcher)
        {
            _settingsManager = settingsManager;
            _devices = devices;
            _scanner = scanner;
            _thumbnails = thumbnails;
            _history = history;
            _dispatcher = dispatcher;

            MediaLibrary = new LibraryViewModel(LibraryKind.Media, settingsManager, thumbnails, StartTransfer, OpenPreview, OnSelectionChanged, AskToDelete);
            FilesLibrary = new LibraryViewModel(LibraryKind.Files, settingsManager, thumbnails, StartTransfer, OpenPreview, OnSelectionChanged, AskToDelete);
            foreach (var library in Libraries)
            {
                var lib = library;
                lib.SearchChanged += () =>
                {
                    _pendingSearch.Add(lib);
                    _searchDebounce!.Stop();
                    _searchDebounce.Start();
                };
            }

            Transfer = new TransferViewModel(transferEngine, () => Settings, () => _activeDevice, OnTransferFinished, OnTransferStartFailed);

            _selectedTabIndex = Math.Clamp(Settings.SelectedTab, 0, 1);
            _destinationFolder = Settings.EffectiveDestination;
            _showFirstRun = !Settings.FirstRunCompleted;
            _thumbnails.Enabled = Settings.GenerateThumbnails;

            _flushTimer = new DispatcherTimer(DispatcherPriority.Background, dispatcher) { Interval = FlushInterval };
            _flushTimer.Tick += (_, _) => FlushIncoming(force: false);

            _searchDebounce = new DispatcherTimer(DispatcherPriority.Background, dispatcher) { Interval = TimeSpan.FromMilliseconds(250) };
            _searchDebounce.Tick += (_, _) =>
            {
                _searchDebounce.Stop();
                foreach (var library in _pendingSearch) library.RefreshView();
                _pendingSearch.Clear();
            };

            _deviceChangeDebounce = new DispatcherTimer(DispatcherPriority.Background, dispatcher) { Interval = TimeSpan.FromMilliseconds(700) };
            _deviceChangeDebounce.Tick += (_, _) =>
            {
                _deviceChangeDebounce.Stop();
                _ = _devices.RefreshAsync();
            };

            _devices.Changed += (_, _) => _dispatcher.BeginInvoke(SyncDeviceState);
            Transfer.PropertyChanged += (_, e) =>
            {
                if (e.PropertyName != nameof(TransferViewModel.IsRunning)) return;
                _thumbnails.Suspended = Transfer.IsRunning; // copying gets the whole phone connection
                UpdateEnvironment();
            };

            foreach (var library in Libraries) library.RefreshView();
            UpdatePanels();
            UpdateEnvironment();
        }

        private AppSettings Settings => _settingsManager.Current;

        public LibraryViewModel MediaLibrary { get; }
        public LibraryViewModel FilesLibrary { get; }
        public IReadOnlyList<LibraryViewModel> Libraries => new[] { MediaLibrary, FilesLibrary };
        public LibraryViewModel SelectedLibrary => SelectedTabIndex == 1 ? FilesLibrary : MediaLibrary;
        public TransferViewModel Transfer { get; }

        [ObservableProperty]
        [NotifyPropertyChangedFor(nameof(SelectedLibrary))]
        private int _selectedTabIndex;

        partial void OnSelectedTabIndexChanged(int value)
        {
            Settings.SelectedTab = value;
            _settingsManager.Save();
            UpdateStatus();
        }

        // ---- Device / connection -------------------------------------------------------------

        [ObservableProperty] private ConnectionState _connectionState = ConnectionState.Waiting;
        [ObservableProperty] private string _deviceName = "No phone connected";
        [ObservableProperty] private string _connectionText = "Not connected";
        [ObservableProperty] private IReadOnlyList<PhoneListing> _phones = Array.Empty<PhoneListing>();
        [ObservableProperty] private string? _selectedPhoneId;
        [ObservableProperty] private bool _hasMultiplePhones;
        [ObservableProperty] private string? _deviceErrorMessage;
        [ObservableProperty] private bool _isScanning;
        [ObservableProperty] private string _statusText = "Starting…";

        [ObservableProperty] private bool _showWaitingPanel;
        [ObservableProperty] private bool _showConnectingPanel;
        [ObservableProperty] private bool _showLockedPanel;
        [ObservableProperty] private bool _showErrorPanel;
        [ObservableProperty] private bool _showFirstRun;

        [ObservableProperty] private string _destinationFolder;
        [ObservableProperty] private bool _isIdle = true;

        [ObservableProperty] private bool _showMessage;
        [ObservableProperty] private string _messageTitle = "";
        [ObservableProperty] private string _messageBody = "";
        [ObservableProperty] private bool _messageOffersFolderChange;
        [ObservableProperty]
        [NotifyPropertyChangedFor(nameof(HasConfirm))]
        private string? _messageConfirmText;

        [ObservableProperty]
        [NotifyPropertyChangedFor(nameof(HasWarning))]
        private string? _messageWarning;

        public bool HasConfirm => MessageConfirmText != null;
        public bool HasWarning => MessageWarning != null;
        private Func<Task>? _pendingConfirm;

        public event Action<PreviewViewModel>? PreviewRequested;
        public event Action<SettingsViewModel>? SettingsRequested;

        // =====================================================================================
        // Lifecycle
        // =====================================================================================

        public void Start()
        {
            Logger.Info("Watching for phones");
            _ = _devices.RefreshAsync();
        }

        public void Shutdown()
        {
            StopScan();
            Transfer.Cancel();
        }

        /// <summary>Called by the window for every WM_DEVICECHANGE; debounced because Windows sends bursts.</summary>
        public void OnDeviceChangeNotification()
        {
            _deviceChangeDebounce.Stop();
            _deviceChangeDebounce.Start();
        }

        /// <summary>Lets the thumbnail loader ask the grids whether a tile is really on screen.</summary>
        public void SetThumbnailVisibilityProbe(Func<MediaItemViewModel, bool> isOnScreen) =>
            _thumbnails.IsOnScreen = target => target is MediaItemViewModel vm && isOnScreen(vm);

        // =====================================================================================
        // Device state
        // =====================================================================================

        private void SyncDeviceState()
        {
            var state = _devices.State;
            var device = _devices.Current;

            Phones = _devices.Phones;
            HasMultiplePhones = Phones.Count > 1;
            _syncingPhone = true;
            SelectedPhoneId = device?.Info.DeviceId ?? Phones.FirstOrDefault()?.DeviceId;
            _syncingPhone = false;

            if (!ReferenceEquals(device, _activeDevice))
            {
                StopScan();
                foreach (var library in Libraries) library.Clear();
                _activeDevice = device;
                _thumbnails.SetDevice(null);
            }

            ConnectionState = state;
            DeviceErrorMessage = _devices.ErrorMessage;
            DeviceName = device?.Info.Name ?? Phones.FirstOrDefault()?.Name ?? "No phone connected";
            ConnectionText = state switch
            {
                ConnectionState.Ready => "Connected",
                ConnectionState.Connecting => "Connecting…",
                ConnectionState.Locked => "Waiting for access",
                ConnectionState.Error => "Connection problem",
                _ => "Not connected"
            };

            if (state == ConnectionState.Ready && device != null && !ReferenceEquals(_scannedDevice, device))
            {
                StartScan(device);
            }

            Transfer.OnDeviceChanged(device, state);
            UpdatePanels();
            UpdateEnvironment();
        }

        partial void OnSelectedPhoneIdChanged(string? value)
        {
            if (_syncingPhone || value == null || Transfer.IsRunning) return;
            _ = _devices.SelectPhoneAsync(value);
        }

        private void UpdatePanels()
        {
            var state = ConnectionState;
            ShowWaitingPanel = state == ConnectionState.Waiting;
            ShowConnectingPanel = state == ConnectionState.Connecting;
            ShowLockedPanel = state == ConnectionState.Locked;
            ShowErrorPanel = state == ConnectionState.Error;
        }

        /// <summary>Pushes connection/scan/transfer state into both tabs and the status bar.</summary>
        private void UpdateEnvironment()
        {
            bool ready = ConnectionState == ConnectionState.Ready && _activeDevice != null;
            IsIdle = !Transfer.IsRunning;
            foreach (var library in Libraries) library.SetEnvironment(ready, IsIdle, IsScanning, _scanError);
            UpdateStatus();
        }

        private void UpdateStatus()
        {
            var media = MediaLibrary.ItemCount;
            var files = FilesLibrary.ItemCount;
            StatusText = ConnectionState switch
            {
                ConnectionState.Waiting => "Waiting for a phone — connect it with a USB cable",
                ConnectionState.Connecting => $"Connecting to {DeviceName}…",
                ConnectionState.Locked => $"{DeviceName} detected — unlock it and choose File Transfer",
                ConnectionState.Error => DeviceErrorMessage ?? "Connection problem",
                _ when IsScanning => $"Scanning phone…  {media:N0} photos/videos and {files:N0} files found so far",
                _ when _scanError != null => $"Scan stopped: {_scanError}",
                _ => $"Connected  •  {media:N0} photos & videos  •  {files:N0} files" +
                     (SelectedLibrary.NewItemsCount > 0 ? $"  •  {SelectedLibrary.NewItemsCount:N0} not on this PC yet" : "")
            };
        }

        private void OnSelectionChanged() => UpdateStatus();

        // =====================================================================================
        // Scanning
        // =====================================================================================

        private void StartScan(IWpdAdapter device)
        {
            _scannedDevice = device;
            _scanCts = new CancellationTokenSource();
            var token = _scanCts.Token;
            var roots = _devices.StorageRoots;

            _transferredKeys = LoadHistory(device.Info.Key);
            _thumbnails.SetDevice(device);
            _scanError = null;
            IsScanning = true;
            _scanStopwatch.Restart();
            _flushTimer.Start();
            Logger.Info($"Scan started on {device.Info.Name}");

            _ = Task.Run(async () =>
            {
                string? error = null;
                try
                {
                    await _scanner.ScanAsync(device, roots, batch => _incoming.Enqueue(batch), token);
                }
                catch (OperationCanceledException)
                {
                    return;
                }
                catch (Exception ex)
                {
                    Logger.Error("Scan failed", ex);
                    error = ErrorMessages.Describe(ex);
                }

                await _dispatcher.InvokeAsync(() =>
                {
                    if (token.IsCancellationRequested) return;
                    _flushTimer.Stop();
                    _scanError = error;
                    IsScanning = false;
                    FlushIncoming(force: true);
                    Logger.Info($"Scan finished in {_scanStopwatch.Elapsed.TotalSeconds:F1}s: " +
                                $"{MediaLibrary.ItemCount:N0} photos/videos, {FilesLibrary.ItemCount:N0} files");
                });
            });
        }

        private HashSet<string> LoadHistory(string deviceKey)
        {
            try
            {
                return _history?.GetTransferredKeys(deviceKey) ?? new HashSet<string>(StringComparer.Ordinal);
            }
            catch (Exception ex)
            {
                Logger.Warn("Could not read transfer history", ex);
                return new HashSet<string>(StringComparer.Ordinal);
            }
        }

        private void StopScan()
        {
            _scanCts?.Cancel();
            _scanCts = null;
            _scannedDevice = null;
            _flushTimer.Stop();
            IsScanning = false;
            while (_incoming.TryDequeue(out _)) { }
        }

        private void FlushIncoming(bool force)
        {
            var found = new List<MediaItem>();
            while (_incoming.TryDequeue(out var batch)) found.AddRange(batch);

            if (found.Count > 0)
            {
                foreach (var library in Libraries) library.AddItems(found, _transferredKeys);
            }
            else if (!force)
            {
                return;
            }

            int total = FilesLibrary.ItemCount;
            var refreshInterval = total < 2000 ? TimeSpan.FromMilliseconds(400) : TimeSpan.FromSeconds(2);
            if (force || DateTime.UtcNow - _lastViewRefresh >= refreshInterval)
            {
                foreach (var library in Libraries) library.RefreshView();
                _lastViewRefresh = DateTime.UtcNow;
            }

            UpdateEnvironment();
        }

        /// <summary>
        /// Refresh = start a brand-new connection to the phone and rescan it. This also recovers a connection
        /// that died (no need to unplug the phone).
        /// </summary>
        [RelayCommand]
        private Task Refresh() => Transfer.IsRunning ? Task.CompletedTask : _devices.ReconnectAsync();

        [RelayCommand]
        private Task CheckAgain() => _devices.RefreshAsync();

        // =====================================================================================
        // Preview / transfer
        // =====================================================================================

        private void OpenPreview(MediaItemViewModel vm, IReadOnlyList<MediaItemViewModel> visible)
        {
            if (_activeDevice == null) return;
            var list = visible.ToList();
            int index = list.IndexOf(vm);
            if (index < 0)
            {
                list = new List<MediaItemViewModel> { vm };
                index = 0;
            }
            PreviewRequested?.Invoke(new PreviewViewModel(list, index, _activeDevice, item => StartTransfer(new[] { item })));
        }

        private void StartTransfer(IReadOnlyCollection<MediaItemViewModel> items)
        {
            if (Transfer.IsRunning || items.Count == 0) return;
            if (_activeDevice == null || ConnectionState != ConnectionState.Ready)
            {
                ShowMessageBox("Phone not ready", "Connect and unlock your phone, then try again.");
                return;
            }
            if (IsScanning)
            {
                Logger.Info("Transfer started while the scan is still running; only items found so far are included");
            }

            if (string.IsNullOrWhiteSpace(DestinationFolder) && !ChooseDestination()) return;

            var options = new TransferOptions(DestinationFolder, Settings.SkipExistingFiles, Settings.OrganizeByPhoneFolder);
            _ = Transfer.StartAsync(_activeDevice, items.Select(v => v.Item).ToList(), options);
            UpdateEnvironment();
        }

        private void OnTransferFinished(TransferSummary summary, IReadOnlyList<TransferJob> jobs)
        {
            var done = jobs.Where(j => j.State is TransferState.Complete or TransferState.Skipped).Select(j => j.Item).ToList();
            foreach (var item in done) _transferredKeys.Add(item.HistoryKey);
            var paths = done.Select(i => i.Path).ToList();
            foreach (var library in Libraries) library.MarkTransferred(paths);

            if (summary.DeviceDisconnected || (summary.Remaining > 0 && summary.StopReason != null))
            {
                // The phone dropped out or stopped answering: get a fresh connection right away so Resume works
                // without unplugging the phone.
                Logger.Info("Reconnecting to the phone after an interrupted transfer");
                _ = _devices.ReconnectAsync();
            }
            UpdateEnvironment();
        }

        private void OnTransferStartFailed(Exception ex)
        {
            if (ex is InsufficientSpaceException space)
            {
                ShowMessageBox("Not enough free space",
                    $"Selected: {FileUtilities.FormatBytes(space.Required)}\n" +
                    $"Available: {FileUtilities.FormatBytes(space.Available)}\n" +
                    $"Additional space required: {FileUtilities.FormatBytes(Math.Max(0, space.Required - space.Available))}",
                    offerFolderChange: true);
            }
            else
            {
                ShowMessageBox("The transfer couldn't start", ErrorMessages.Describe(ex),
                    offerFolderChange: ex is UnauthorizedAccessException or IOException);
            }
        }

        // =====================================================================================
        // Destination / settings / dialogs
        // =====================================================================================

        [RelayCommand]
        private void BrowseDestination() => ChooseDestination();

        private bool ChooseDestination()
        {
            var folder = Dialogs.PickFolder(DestinationFolder, "Choose where to save your files");
            if (folder == null) return false;
            DestinationFolder = folder;
            Settings.LastDestination = folder;
            _settingsManager.Save();
            return true;
        }

        [RelayCommand]
        private void OpenDestination() => Dialogs.OpenInExplorer(DestinationFolder);

        [RelayCommand]
        private void OpenSettings()
        {
            SettingsRequested?.Invoke(new SettingsViewModel(_settingsManager, _thumbnails.Cache, ApplySettings));
        }

        private void ApplySettings()
        {
            if (!Transfer.IsRunning) DestinationFolder = Settings.EffectiveDestination;
            _thumbnails.Cache.CapacityBytes = Settings.ThumbnailCacheMegabytes * 1024L * 1024;
            _thumbnails.Enabled = Settings.GenerateThumbnails;
        }

        [RelayCommand]
        private void GetStarted()
        {
            ShowFirstRun = false;
            Settings.FirstRunCompleted = true;
            _settingsManager.Save();
        }

        private void ShowMessageBox(string title, string body, bool offerFolderChange = false)
        {
            MessageTitle = title;
            MessageBody = body;
            MessageWarning = null;
            MessageOffersFolderChange = offerFolderChange;
            MessageConfirmText = null;
            _pendingConfirm = null;
            ShowMessage = true;
        }

        [RelayCommand]
        private void DismissMessage()
        {
            ShowMessage = false;
            _pendingConfirm = null;
        }

        [RelayCommand]
        private async Task ConfirmMessage()
        {
            var action = _pendingConfirm;
            ShowMessage = false;
            _pendingConfirm = null;
            if (action != null) await action();
        }

        // =====================================================================================
        // Delete from phone
        // =====================================================================================

        private void AskToDelete(IReadOnlyCollection<MediaItemViewModel> items)
        {
            if (items.Count == 0 || Transfer.IsRunning || _activeDevice == null) return;
            var device = _activeDevice;
            long bytes = items.Sum(v => v.Item.Size);
            int notOnPc = items.Count(v => !v.AlreadyTransferred);

            MessageTitle = "Delete from phone?";
            MessageBody = $"Permanently delete {items.Count:N0} {(items.Count == 1 ? "item" : "items")} " +
                          $"({FileUtilities.FormatBytes(bytes)}) from {DeviceName}?\n\nThis can't be undone.";
            MessageWarning = notOnPc > 0
                ? $"{notOnPc:N0} of these {(notOnPc == 1 ? "has" : "have")} NOT been copied to this PC yet."
                : null;
            MessageOffersFolderChange = false;
            MessageConfirmText = "Delete";
            _pendingConfirm = () => DeleteAsync(device, items.ToList());
            ShowMessage = true;
        }

        private async Task DeleteAsync(IWpdAdapter device, List<MediaItemViewModel> items)
        {
            Logger.Info($"Deleting {items.Count} item(s) from {device.Info.Name}");
            var deleted = new List<string>();
            var failed = new List<string>();
            IsIdle = false;
            foreach (var library in Libraries) library.SetEnvironment(false, false, IsScanning, _scanError);
            try
            {
                int done = 0;
                foreach (var vm in items)
                {
                    StatusText = $"Deleting from phone…  {++done:N0} / {items.Count:N0}";
                    try
                    {
                        await device.DeleteFileAsync(vm.Item.Path, CancellationToken.None);
                        deleted.Add(vm.Item.Path);
                    }
                    catch (Exception ex)
                    {
                        Logger.Warn($"Could not delete {vm.Item.Name}", ex);
                        failed.Add(vm.Item.Name);
                        if (ex is DeviceDisconnectedException) break;
                    }
                }
            }
            finally
            {
                foreach (var library in Libraries) library.RemoveItems(deleted);
                UpdateEnvironment();
            }

            Logger.Info($"Deleted {deleted.Count} item(s), {failed.Count} failed");
            if (failed.Count > 0)
            {
                ShowMessageBox("Some items weren't deleted",
                    $"Deleted {deleted.Count:N0}. Couldn't delete {failed.Count:N0}:\n" + string.Join("\n", failed.Take(8)) +
                    (failed.Count > 8 ? "\n…" : "") + "\n\nThe phone may protect some files. Unlock it and try again.");
            }
        }

        [RelayCommand]
        private void ChangeFolderFromMessage()
        {
            ShowMessage = false;
            ChooseDestination();
        }
    }
}
