using System.Windows;
using AndroidPhotoTransfer.UI.ViewModels;

namespace AndroidPhotoTransfer.UI.Views
{
    public partial class PreviewWindow : Window
    {
        public PreviewWindow(PreviewViewModel viewModel)
        {
            InitializeComponent();
            DataContext = viewModel;
            viewModel.CloseRequested += Close;
            Closed += (_, _) => viewModel.CloseRequested -= Close;
            Loaded += async (_, _) => await viewModel.LoadAsync();
        }
    }
}
