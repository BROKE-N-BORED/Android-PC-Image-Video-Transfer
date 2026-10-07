using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Windows.Threading;
using AndroidPhotoTransfer.Core.Devices;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Transfer;
using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Infrastructure;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace AndroidPhotoTransfer.UI.ViewModels
{
    /// <summary>Drives the transfer progress panel and the completion panel.</summary>
    public sealed partial class TransferViewModel : ObservableObject
    {
        private static readonly TimeSpan UiInterval = TimeSpan.FromMilliseconds(400);

        private readonly ITransferManager _engine;
        private readonly Func<AppSettings> _settings;
        private readonly Func<IWpdAdapter?> _currentDevice;
        private readonly Action<TransferSummary, IReadOnlyList<TransferJob>> _onFinished;
        private readonly Action<Exception> _onStartFailed;
        private readonly DispatcherTimer _timer;
        private readonly Stopwatch _elapsed = new();

        private List<TransferJob> _jobs = new();
        private TransferOptions? _options;
        private string? _deviceKey;
        private TransferProgress _progress = new();
        private PauseGate _pause = new();
        private CancellationTokenSource? _cts;
        private long _lastSampleBytes;
        private TimeSpan _lastSampleTime;
        private double _bytesPerSecond;

        internal TransferViewModel(ITransferManager engine, Func<AppSettings> settings, Func<IWpdAdapter?> currentDevice,
            Action<TransferSummary, IReadOnlyList<TransferJob>> onFinished, Action<Exception> onStartFailed)
        {
            _engine = engine;
            _settings = settings;
            _currentDevice = currentDevice;
            _onFinished = onFinished;
            _onStartFailed = onStartFailed;
            _timer = new DispatcherTimer { Interval = UiInterval };
            _timer.Tick += (_, _) => UpdateProgress();
        }

        [ObservableProperty] private bool _isVisible;
        [ObservableProperty] private bool _isRunning;
        [ObservableProperty] private bool _isComplete;
        [ObservableProperty] private bool _isPaused;
        [ObservableProperty] private string _title = "Transferring";
        [ObservableProperty] private string _currentFile = "";
        [ObservableProperty] private double _overallPercent;
        [ObservableProperty] private double _filePercent;
        [ObservableProperty] private string _percentText = "0%";
        [ObservableProperty] private string _filesText = "";
        [ObservableProperty] private string _bytesText = "";
        [ObservableProperty] private string _speedText = "";
        [ObservableProperty] private string _elapsedText = "00:00";
        [ObservableProperty] private string _remainingText = "";
        [ObservableProperty] private string _pauseButtonText = "Pause";
        [ObservableProperty] private int _liveCopied;
        [ObservableProperty] private int _liveSkipped;
        [ObservableProperty] private int _liveFailed;

        [ObservableProperty] private string _completionTitle = "";
        [ObservableProperty] private string _completionSummary = "";
        [ObservableProperty] private string _completionCounts = "";
        [ObservableProperty] private string _completionDetail = "";
        [ObservableProperty] private bool _canRetryFailed;
        [ObservableProperty] private bool _canResume;
        [ObservableProperty] private bool _resumeAvailable;
        [ObservableProperty] private bool _hasFailures;
        [ObservableProperty] private bool _showDetails;

        public ObservableCollection<string> FailureDetails { get; } = new();

        internal async Task StartAsync(IWpdAdapter device, IReadOnlyList<MediaItem> items, TransferOptions options)
        {
            if (IsRunning) return;
            _jobs = items.Select(i => new TransferJob(i)).ToList();
            _options = options;
            await RunAsync(device);
        }

        private async Task RunAsync(IWpdAdapter device)
        {
            _deviceKey = device.Info.Key;
            _cts = new CancellationTokenSource();
            _pause = new PauseGate();
            _progress = new TransferProgress();
            _lastSampleBytes = 0;
            _lastSampleTime = TimeSpan.Zero;
            _bytesPerSecond = 0;

            Title = "Transferring";
            CurrentFile = "Preparing…";
            OverallPercent = FilePercent = 0;
            PercentText = "0%";
            SpeedText = RemainingText = "";
            ElapsedText = "00:00";
            PauseButtonText = "Pause";
            LiveCopied = LiveSkipped = LiveFailed = 0;
            IsPaused = false;
            IsComplete = false;
            ShowDetails = false;
            IsRunning = true;
            IsVisible = true;
            _elapsed.Restart();
            _timer.Start();

            TransferSummary summary;
            var options = _options!;
            var token = _cts.Token;
            try
            {
                summary = await Task.Run(() => _engine.RunAsync(device, _jobs, options, _progress, _pause, token));
            }
            catch (Exception ex)
            {
                _timer.Stop();
                _elapsed.Stop();
                IsRunning = false;
                IsVisible = false;
                Logger.Error("Transfer could not start", ex);
                _onStartFailed(ex);
                return;
            }

            _timer.Stop();
            _elapsed.Stop();
            UpdateProgress();
            IsRunning = false;
            IsPaused = false;
            ShowCompletion(summary);
            _onFinished(summary, _jobs);
        }

        private void ShowCompletion(TransferSummary summary)
        {
            if (summary.DeviceDisconnected) CompletionTitle = "PHONE DISCONNECTED";
            else if (summary.StopReason != null) CompletionTitle = "TRANSFER STOPPED";
            else if (summary.Cancelled > 0) CompletionTitle = "TRANSFER CANCELLED";
            else CompletionTitle = "TRANSFER COMPLETE";

            CompletionSummary = $"{summary.Copied:N0} {(summary.Copied == 1 ? "file" : "files")} copied  •  {FileUtilities.FormatBytes(summary.BytesCopied)}";

            var counts = new List<string>
            {
                $"Skipped: {summary.Skipped:N0}",
                $"Failed: {summary.Failed:N0}"
            };
            if (summary.Cancelled > 0) counts.Add($"Not copied: {summary.Cancelled:N0}");
            if (summary.Remaining > 0) counts.Add($"Remaining: {summary.Remaining:N0}");
            CompletionCounts = string.Join("     ", counts);

            if (summary.DeviceDisconnected)
            {
                CompletionDetail = $"Files already copied are safe. Reconnect your phone, unlock it and choose \"File Transfer\", then click Resume to copy the remaining {summary.Remaining:N0}.";
            }
            else if (summary.StopReason != null)
            {
                CompletionDetail = summary.StopReason;
            }
            else if (summary.Skipped > 0)
            {
                CompletionDetail = "Skipped files were already on this PC, so they weren't copied again.";
            }
            else
            {
                CompletionDetail = "";
            }

            FailureDetails.Clear();
            foreach (var job in _jobs.Where(j => j.State == TransferState.Failed))
            {
                FailureDetails.Add($"{job.Item.Name} — {job.Error}");
            }
            HasFailures = FailureDetails.Count > 0;
            CanRetryFailed = summary.Failed > 0;
            CanResume = _jobs.Any(j => j.State == TransferState.Pending);
            UpdateResumeAvailability();
            IsComplete = true;

            if (_settings().OpenFolderAfterTransfer && summary.Copied > 0 && summary.Failed == 0 &&
                !summary.DeviceDisconnected && summary.StopReason == null && summary.Cancelled == 0)
            {
                OpenFolder();
            }
        }

        private void UpdateProgress()
        {
            var s = _progress.Snapshot();
            if (s.CurrentFile.Length > 0) CurrentFile = s.CurrentFile;
            FilePercent = s.CurrentFileSize > 0 ? Math.Min(100, s.CurrentFileBytes * 100.0 / s.CurrentFileSize) : 0;
            OverallPercent = s.BytesTotal > 0
                ? Math.Min(100, s.BytesProcessed * 100.0 / s.BytesTotal)
                : s.FilesTotal > 0 ? s.FilesDone * 100.0 / s.FilesTotal : 0;
            PercentText = $"{OverallPercent:F0}%";
            FilesText = $"{s.FilesDone:N0} / {s.FilesTotal:N0} files";
            BytesText = $"{FileUtilities.FormatBytes(s.BytesProcessed)} / {FileUtilities.FormatBytes(s.BytesTotal)}";
            LiveCopied = s.Copied;
            LiveSkipped = s.Skipped;
            LiveFailed = s.Failed;

            var now = _elapsed.Elapsed;
            var dt = (now - _lastSampleTime).TotalSeconds;
            if (dt >= 0.35)
            {
                var instant = Math.Max(0, s.BytesCopied - _lastSampleBytes) / dt;
                _bytesPerSecond = _bytesPerSecond <= 0 ? instant : _bytesPerSecond * 0.7 + instant * 0.3;
                _lastSampleBytes = s.BytesCopied;
                _lastSampleTime = now;
            }

            ElapsedText = FormatDuration(now);
            if (IsPaused)
            {
                SpeedText = "Paused";
                RemainingText = "—";
            }
            else
            {
                SpeedText = $"{FileUtilities.FormatBytes((long)_bytesPerSecond)}/s";

                // Estimate from the whole run so far, weighting files and bytes equally: many small photos are
                // slow per byte and big videos are fast, so a speed-only estimate swings wildly between them.
                double fileFraction = s.FilesTotal > 0 ? (double)s.FilesDone / s.FilesTotal : 0;
                double byteFraction = s.BytesTotal > 0 ? (double)s.BytesProcessed / s.BytesTotal : fileFraction;
                double done = 0.5 * fileFraction + 0.5 * byteFraction;
                RemainingText = done > 0.003 && now.TotalSeconds > 8
                    ? "~" + FormatDuration(TimeSpan.FromSeconds(now.TotalSeconds * (1 - done) / done))
                    : "Calculating…";
            }
        }

        private static string FormatDuration(TimeSpan time) =>
            time.TotalHours >= 1 ? $"{(int)time.TotalHours}:{time.Minutes:00}:{time.Seconds:00}" : $"{time.Minutes:00}:{time.Seconds:00}";

        internal void OnDeviceChanged(IWpdAdapter? device, ConnectionState state) => UpdateResumeAvailability(device, state);

        private void UpdateResumeAvailability(IWpdAdapter? device = null, ConnectionState? state = null)
        {
            device ??= _currentDevice();
            ResumeAvailable = device != null && device.Info.Key == _deviceKey && (state ?? ConnectionState.Ready) == ConnectionState.Ready;
        }

        [RelayCommand]
        private void TogglePause()
        {
            if (!IsRunning) return;
            if (IsPaused)
            {
                _pause.Resume();
                _elapsed.Start();
                IsPaused = false;
                PauseButtonText = "Pause";
                Title = "Transferring";
            }
            else
            {
                _pause.Pause();
                _elapsed.Stop();
                IsPaused = true;
                PauseButtonText = "Resume";
                Title = "Paused — the current file will start over when you resume";
            }
            UpdateProgress();
        }

        [RelayCommand]
        public void Cancel()
        {
            if (!IsRunning || _cts == null) return;
            Title = "Cancelling…";
            _cts.Cancel();
            _pause.Resume();
        }

        [RelayCommand]
        private async Task RetryFailed()
        {
            foreach (var job in _jobs.Where(j => j.State == TransferState.Failed))
            {
                job.State = TransferState.Pending;
                job.Error = null;
            }
            await ResumeAsync();
        }

        [RelayCommand]
        private Task Resume() => ResumeAsync();

        private async Task ResumeAsync()
        {
            var device = _currentDevice();
            if (device == null || device.Info.Key != _deviceKey || _options == null)
            {
                CompletionDetail = "Connect the same phone to continue. Unlock it and choose \"File Transfer\" if asked.";
                return;
            }
            foreach (var job in _jobs.Where(j => j.State == TransferState.Cancelled)) job.State = TransferState.Pending;
            await RunAsync(device);
        }

        [RelayCommand]
        private void ToggleDetails() => ShowDetails = !ShowDetails;

        [RelayCommand]
        private void OpenFolder()
        {
            if (_options != null) Dialogs.OpenInExplorer(_options.DestinationFolder);
        }

        [RelayCommand]
        private void Done()
        {
            IsVisible = false;
            IsComplete = false;
        }
    }
}
