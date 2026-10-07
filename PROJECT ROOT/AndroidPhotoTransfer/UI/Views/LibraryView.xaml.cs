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

        /// <summary>
        /// Puts keyboard focus on the items, so Windows doesn't hand it to the folder tree
        /// (a focused tree auto-selects its first folder and changes what's shown).
        /// </summary>
        public void FocusItems()
        {
            if (ItemGrid.IsVisible) ItemGrid.Focus();
            else if (ItemList.IsVisible) ItemList.Focus();
        }

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

        /// <summary>A click anywhere on a row ticks it (Shift+click ticks a range, double-click previews) — like the tiles.</summary>
        private void ListRow_PreviewMouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (sender is not ListViewItem row || row.DataContext is not MediaItemViewModel item) return;

            // Let the row's own checkbox handle clicks on itself.
            for (var element = e.OriginalSource as DependencyObject; element != null && element != row;
                 element = VisualTreeHelper.GetParent(element))
            {
                if (element is CheckBox) return;
            }

            e.Handled = true;
            row.Focus();
            Library?.OnTileClicked(item, Keyboard.Modifiers.HasFlag(ModifierKeys.Shift), e.ClickCount);
        }

        private void ItemList_HeaderClick(object sender, RoutedEventArgs e)
        {
            if (e.OriginalSource is GridViewColumnHeader { Column.Header: string header }) Library?.SortByColumn(header);
        }

        private void ItemList_PreviewKeyDown(object sender, KeyEventArgs e)
        {
            var focused = (Keyboard.FocusedElement as ListViewItem)?.DataContext as MediaItemViewModel;
            var targets = ItemList.SelectedItems.OfType<MediaItemViewModel>().ToList();
            if (targets.Count == 0 && focused != null) targets.Add(focused);

            if (e.Key == Key.Space && targets.Count > 0)
            {
                Library?.ToggleChecked(targets);
                e.Handled = true;
            }
            else if (e.Key == Key.Enter && targets.Count > 0)
            {
                Library?.OpenPreview(targets[0]);
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
