using System.Windows;
using AndroidPhotoTransfer.UI.ViewModels;

namespace AndroidPhotoTransfer.UI.Views
{
    public partial class SettingsWindow : Window
    {
        public SettingsWindow(SettingsViewModel viewModel)
        {
            InitializeComponent();
            DataContext = viewModel;
            viewModel.CloseRequested += Close;
            Closed += (_, _) => viewModel.CloseRequested -= Close;
        }
    }
}
