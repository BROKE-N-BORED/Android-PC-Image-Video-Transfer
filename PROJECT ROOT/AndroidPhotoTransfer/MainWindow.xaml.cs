using System.ComponentModel;
using System.Windows;
using System.Windows.Interop;
using AndroidPhotoTransfer.Infrastructure;
using AndroidPhotoTransfer.UI.ViewModels;
using AndroidPhotoTransfer.UI.Views;

namespace AndroidPhotoTransfer
{
    public partial class MainWindow : Window
    {
        private const int WmDeviceChange = 0x0219;

        private readonly MainViewModel _viewModel;
        private readonly SettingsManager _settings;

        internal MainWindow(MainViewModel viewModel, SettingsManager settings)
        {
            _viewModel = viewModel;
            _settings = settings;
            InitializeComponent();
            DataContext = viewModel;
            RestorePlacement();

            // Thumbnails are only fetched for tiles that are really on screen in the visible tab.
            viewModel.SetThumbnailVisibilityProbe(item => MediaView.IsTileOnScreen(item) || FilesView.IsTileOnScreen(item));

            viewModel.PreviewRequested += preview =>
            {
                var window = new PreviewWindow(preview) { Owner = this };
                window.ShowDialog();
            };
            viewModel.SettingsRequested += settingsViewModel =>
            {
                var window = new SettingsWindow(settingsViewModel) { Owner = this };
                window.ShowDialog();
            };
        }

        protected override void OnSourceInitialized(EventArgs e)
        {
            base.OnSourceInitialized(e);
            // Windows broadcasts WM_DEVICECHANGE to top-level windows when USB devices come and go,
            // so the program reacts to phones instantly without polling.
            HwndSource.FromHwnd(new WindowInteropHelper(this).Handle)?.AddHook(WndProc);
            if (_settings.Current.WindowMaximized) WindowState = WindowState.Maximized;
        }

        private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            if (msg == WmDeviceChange) _viewModel.OnDeviceChangeNotification();
            return IntPtr.Zero;
        }

        private void RestorePlacement()
        {
            var s = _settings.Current;
            if (s.WindowWidth is not > 0 || s.WindowHeight is not > 0 || s.WindowLeft is null || s.WindowTop is null)
            {
                WindowStartupLocation = WindowStartupLocation.CenterScreen;
                return;
            }

            var bounds = new Rect(s.WindowLeft.Value, s.WindowTop.Value, s.WindowWidth.Value, s.WindowHeight.Value);
            var screen = new Rect(SystemParameters.VirtualScreenLeft, SystemParameters.VirtualScreenTop,
                SystemParameters.VirtualScreenWidth, SystemParameters.VirtualScreenHeight);
            if (!screen.IntersectsWith(bounds) || bounds.Width < MinWidth || bounds.Height < MinHeight)
            {
                WindowStartupLocation = WindowStartupLocation.CenterScreen;
                return;
            }

            WindowStartupLocation = WindowStartupLocation.Manual;
            Left = bounds.Left;
            Top = bounds.Top;
            Width = bounds.Width;
            Height = bounds.Height;
        }

        protected override void OnClosing(CancelEventArgs e)
        {
            if (_viewModel.Transfer.IsRunning)
            {
                var answer = MessageBox.Show(this,
                    "A transfer is still running. Stop it and close the program?\n\nFiles that were already copied are kept.",
                    "Android Photo Transfer", MessageBoxButton.YesNo, MessageBoxImage.Warning);
                if (answer != MessageBoxResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
                _viewModel.Transfer.Cancel();
            }

            var bounds = WindowState == WindowState.Normal ? new Rect(Left, Top, Width, Height) : RestoreBounds;
            var s = _settings.Current;
            s.WindowLeft = bounds.Left;
            s.WindowTop = bounds.Top;
            s.WindowWidth = bounds.Width;
            s.WindowHeight = bounds.Height;
            s.WindowMaximized = WindowState == WindowState.Maximized;
            _settings.Save();

            base.OnClosing(e);
        }
    }
}
