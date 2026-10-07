using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using AndroidPhotoTransfer.UI.ViewModels;

namespace AndroidPhotoTransfer.UI.Views
{
    /// <summary>One tab's browser: sidebar, toolbar, thumbnail grid and list.</summary>
    public partial class LibraryView : UserControl
    {
        public LibraryView()
        {
            InitializeComponent();
        }

        private LibraryViewModel? Library => DataContext as LibraryViewModel;

        /// <summary>True when the item's tile currently exists in this view's visible grid.</summary>
        public bool IsTileOnScreen(MediaItemViewModel item) =>
            IsVisible && ItemGrid.IsVisible && ItemGrid.ItemContainerGenerator.ContainerFromItem(item) != null;

        // ---- Grid tiles --------------------------------------------------------------------------

        private static MediaItemViewModel? ItemOf(object sender) => (sender as FrameworkElement)?.DataContext as MediaItemViewModel;

        private void Tile_Loaded(object sender, RoutedEventArgs e)
        {
            if (ItemOf(sender) is { } item) Library?.OnTileRealized(item, sender);
        }

        private void Tile_Unloaded(object sender, RoutedEventArgs e)
        {
            ItemOf(sender)?.OnUnrealized(sender);
        }

        private void Tile_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (ItemOf(sender) is not { } item) return;
            e.Handled = true;
            Library?.OnTileClicked(item, Keyboard.Modifiers.HasFlag(ModifierKeys.Shift), e.ClickCount);
        }

        private void PreviewMenu_Click(object sender, RoutedEventArgs e)
        {
            if (ItemOf(sender) is { } item) Library?.OpenPreview(item);
        }

        private void TransferMenu_Click(object sender, RoutedEventArgs e)
        {
            if (ItemOf(sender) is { } item) Library?.TransferItem(item);
        }

        private void ToggleMenu_Click(object sender, RoutedEventArgs e)
        {
            if (ItemOf(sender) is { } item) item.IsChecked = !item.IsChecked;
        }

        private void ShowFolderMenu_Click(object sender, RoutedEventArgs e)
        {
            if (ItemOf(sender) is { } item) Library?.ShowFolderOf(item);
        }

        // ---- List view ---------------------------------------------------------------------------

        private void ItemList_MouseDoubleClick(object sender, MouseButtonEventArgs e)
        {
            var element = e.OriginalSource as DependencyObject;
            while (element != null && element is not ListViewItem) element = VisualTreeHelper.GetParent(element);
            if ((element as ListViewItem)?.DataContext is MediaItemViewModel item) Library?.OpenPreview(item);
        }

        private void ItemList_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            var selected = ItemList.SelectedItems.OfType<MediaItemViewModel>().ToList();
            if (e.Key == Key.Space)
            {
                Library?.ToggleChecked(selected);
                e.Handled = true;
            }
            else if (e.Key == Key.Enter && selected.Count > 0)
            {
                Library?.OpenPreview(selected[0]);
                e.Handled = true;
            }
        }

        // ---- Folder tree -------------------------------------------------------------------------

        private void FolderTree_SelectedItemChanged(object sender, RoutedPropertyChangedEventArgs<object> e)
        {
            Library?.OnFolderSelected(e.NewValue as TreeNodeViewModel);
        }
    }
}
