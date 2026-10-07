using System.Reflection;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;
using AndroidPhotoTransfer.Core.Devices;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Thumbnails;
using AndroidPhotoTransfer.Core.Transfer;
using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Data;
using AndroidPhotoTransfer.Infrastructure;
using AndroidPhotoTransfer.UI.ViewModels;

namespace AndroidPhotoTransfer
{
    public partial class App : Application
    {
        private SettingsManager? _settings;
        private MtpWorker? _worker;
        private DeviceManager? _devices;
        private HistoryDatabase? _history;
        private MainViewModel? _viewModel;

        protected override void OnStartup(StartupEventArgs e)
        {
            base.OnStartup(e);

            Logger.Initialize(AppPaths.LogsFolder);
            Logger.Info($"Application startup (v{Assembly.GetExecutingAssembly().GetName().Version}, {Environment.OSVersion})");

            DispatcherUnhandledException += OnDispatcherUnhandledException;
            TaskScheduler.UnobservedTaskException += (_, args) =>
            {
                Logger.Error("Unobserved background exception", args.Exception);
                args.SetObserved();
            };
            AppDomain.CurrentDomain.UnhandledException += (_, args) =>
                Logger.Error("Fatal unhandled exception", args.ExceptionObject as Exception);

            var startup = System.Diagnostics.Stopwatch.StartNew();
            _settings = new SettingsManager(AppPaths.SettingsFile);
            _worker = new MtpWorker();

            try
            {
                _history = new HistoryDatabase(AppPaths.HistoryDatabase);
            }
            catch (Exception ex)
            {
                // The app still works without history; only "Import New" loses its memory.
                Logger.Error("Transfer history database is unavailable", ex);
            }
            Logger.Info($"Settings and history loaded in {startup.ElapsedMilliseconds} ms");

            // Developer switch: also list USB drives (they appear as WPD devices) to test without a phone.
            bool includeStorage = Environment.GetEnvironmentVariable("APT_INCLUDE_STORAGE_DEVICES") == "1";
            // Developer switch: treat a normal folder as a phone (APT_DEMO_FOLDER=C:\some\folder).
            _devices = new DeviceManager(_worker, includeStorage, Environment.GetEnvironmentVariable("APT_DEMO_FOLDER"));

            var cache = new ThumbnailCache(_settings.Current.ThumbnailCacheMegabytes * 1024L * 1024);
            var thumbnails = new ThumbnailManager(cache, Dispatcher);

            _viewModel = new MainViewModel(_settings, _devices, new MediaScanner(), thumbnails,
                new TransferManager(_history), _history, Dispatcher);

            var window = new MainWindow(_viewModel, _settings);
            MainWindow = window;
            window.Show();
            Logger.Info($"Main window shown {startup.ElapsedMilliseconds} ms after startup");
            _viewModel.Start();
        }

        protected override void OnExit(ExitEventArgs e)
        {
            try
            {
                _viewModel?.Shutdown();
                _devices?.Dispose();
                _worker?.Dispose();
                _history?.Dispose();
                _settings?.Save();
            }
            catch (Exception ex)
            {
                Logger.Warn("Error during shutdown", ex);
            }
            Logger.Info("Application exit");
            base.OnExit(e);
        }

        private void OnDispatcherUnhandledException(object sender, DispatcherUnhandledExceptionEventArgs e)
        {
            Logger.Error("Unexpected error", e.Exception);
            MessageBox.Show(
                "Something unexpected went wrong, but your photos are safe.\n\n" +
                ErrorMessages.Describe(e.Exception) + "\n\nTechnical details were saved to the log (Settings → Open Logs).",
                "Android Photo Transfer", MessageBoxButton.OK, MessageBoxImage.Warning);
            e.Handled = true;
        }
    }
}
