using System.Collections.ObjectModel;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Thumbnails;
using AndroidPhotoTransfer.Infrastructure;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace AndroidPhotoTransfer.UI.ViewModels
{
    public enum LibraryKind
    {
        /// <summary>Photos & Videos tab.</summary>
        Media,
        /// <summary>Files tab: every file on the phone.</summary>
        Files
    }

    /// <summary>
    /// One tab: its own sidebar (categories + folders), search/sort/filters, grid/list and selection.
    /// Both tabs share the phone connection, the scan results and the transfer engine.
    /// </summary>
    public sealed partial class LibraryViewModel : ObservableObject
    {
        private readonly SettingsManager _settingsManager;
        private readonly ThumbnailManager _thumbnails;
        private readonly Action<IReadOnlyCollection<MediaItemViewModel>> _startTransfer;
        private readonly Action<MediaItemViewModel, IReadOnlyList<MediaItemViewModel>> _openPreview;
        private readonly Action _selectionChanged;

        private readonly List<MediaItemViewModel> _allItems = new();
        private readonly Dictionary<string, MediaItemViewModel> _itemsByPath = new(StringComparer.Ordinal);
        private readonly Dictionary<string, TreeNodeViewModel> _storageNodes = new(StringComparer.OrdinalIgnoreCase);
        private MediaItemViewModel? _selectionAnchor;
        private long _selectedBytes;
        private bool _selectionBatch;
        private bool _ready;
        private bool _idle = true;
        private bool _scanning;
        private string? _scanError;

        internal LibraryViewModel(LibraryKind kind, SettingsManager settingsManager, ThumbnailManager thumbnails,
            Action<IReadOnlyCollection<MediaItemViewModel>> startTransfer,
            Action<MediaItemViewModel, IReadOnlyList<MediaItemViewModel>> openPreview,
            Action selectionChanged)
        {
            Kind = kind;
            _settingsManager = settingsManager;
            _thumbnails = thumbnails;
            _startTransfer = startTransfer;
            _openPreview = openPreview;
            _selectionChanged = selectionChanged;

            if (kind == LibraryKind.Media)
            {
                Header = "Photos & Videos";
                ItemNoun = "photos and videos";
                Categories = new ObservableCollection<CategoryViewModel>
                {
                    new("All Photos & Videos", "", null),
                    new("Camera", "", m => m.Category == MediaCategory.Camera),
                    new("Screenshots", "", m => m.Category == MediaCategory.Screenshots),
                    new("Pictures", "", m => m.Category == MediaCategory.Pictures),
                    new("Downloads", "", m => m.Category == MediaCategory.Downloads),
                    new("Videos", "", m => m.Kind == MediaKind.Video),
                    new("Other Media", "", m => m.Category == MediaCategory.Other)
                };
                _sortMode = Settings.SortMode;
                _typeFilter = Settings.TypeFilter;
                _isGridView = Settings.ViewMode == ViewMode.Grid;
            }
            else
            {
                Header = "Files";
                ItemNoun = "files";
                Categories = new ObservableCollection<CategoryViewModel>
                {
                    new("All Files", "", null),
                    new("Photos", "", m => m.FileCategory == FileCategory.Photo),
                    new("Videos", "", m => m.FileCategory == FileCategory.Video),
                    new("Documents", "", m => m.FileCategory == FileCategory.Document),
                    new("Music & Audio", "", m => m.FileCategory == FileCategory.Audio),
                    new("Archives (ZIP)", "", m => m.FileCategory == FileCategory.Archive),
                    new("Apps (APK)", "", m => m.FileCategory == FileCategory.App),
                    new("Downloads Folder", "", m => m.Category == MediaCategory.Downloads),
                    new("Other Files", "", m => m.FileCategory == FileCategory.Other)
                };
                _sortMode = Settings.FilesSortMode;
                _typeFilter = MediaTypeFilter.All;
                _isGridView = Settings.FilesViewMode == ViewMode.Grid;
            }
            _selectedCategory = Categories[0];
            UpdateSelectionText();
        }

        private AppSettings Settings => _settingsManager.Current;

        public LibraryKind Kind { get; }
        public bool IsMediaLibrary => Kind == LibraryKind.Media;
        public string Header { get; }
        public string ItemNoun { get; }

        public ObservableCollection<CategoryViewModel> Categories { get; }
        public ObservableCollection<TreeNodeViewModel> FolderRoots { get; } = new();
        public BulkObservableCollection<MediaItemViewModel> VisibleItems { get; } = new();
        public int ItemCount => _allItems.Count;

        public IReadOnlyList<Option<SortMode>> SortOptions { get; } = new[]
        {
            new Option<SortMode>(SortMode.NewestFirst, "Newest First"),
            new Option<SortMode>(SortMode.OldestFirst, "Oldest First"),
            new Option<SortMode>(SortMode.NameAscending, "Name A–Z"),
            new Option<SortMode>(SortMode.NameDescending, "Name Z–A"),
            new Option<SortMode>(SortMode.LargestFirst, "Largest First"),
            new Option<SortMode>(SortMode.SmallestFirst, "Smallest First")
        };

        public IReadOnlyList<Option<MediaTypeFilter>> TypeOptions { get; } = new[]
        {
            new Option<MediaTypeFilter>(MediaTypeFilter.All, "Photos + Videos"),
            new Option<MediaTypeFilter>(MediaTypeFilter.Photos, "Photos"),
            new Option<MediaTypeFilter>(MediaTypeFilter.Videos, "Videos")
        };

        public IReadOnlyList<Option<TransferStateFilter>> TransferStateOptions { get; } = new[]
        {
            new Option<TransferStateFilter>(TransferStateFilter.All, "Show all"),
            new Option<TransferStateFilter>(TransferStateFilter.NotTransferred, "Not on PC yet"),
            new Option<TransferStateFilter>(TransferStateFilter.Transferred, "Already on PC")
        };

        [ObservableProperty] private CategoryViewModel? _selectedCategory;
        [ObservableProperty] private TreeNodeViewModel? _selectedFolder;
        [ObservableProperty] private string _viewTitle = "";
        [ObservableProperty] private string _searchText = "";
        [ObservableProperty] private SortMode _sortMode;
        [ObservableProperty] private MediaTypeFilter _typeFilter;
        [ObservableProperty] private TransferStateFilter _transferFilter;
        [ObservableProperty] private bool _isGridView;

        [ObservableProperty] private int _selectedCount;
        [ObservableProperty] private int _selectedTransferredCount;
        [ObservableProperty] private string _selectionText = "";
        [ObservableProperty] private string _transferButtonText = "TRANSFER";
        [ObservableProperty] private string _untickTransferredText = "";
        [ObservableProperty] private int _newItemsCount;
        [ObservableProperty] private string _importNewText = "Import New";
        [ObservableProperty] private bool _canTransferSelected;
        [ObservableProperty] private bool _canTransferAll;
        [ObservableProperty] private bool _canImportNew;
        [ObservableProperty] private bool _canSelect;

        [ObservableProperty] private bool _showEmptyPanel;
        [ObservableProperty] private string _emptyMessage = "";

        // =====================================================================================
        // Items (fed by the main view model as the scan finds them)
        // =====================================================================================

        public bool Accepts(MediaItem item) => Kind == LibraryKind.Files || item.IsMedia;

        internal void AddItems(IEnumerable<MediaItem> items, HashSet<string> transferredKeys)
        {
            var categoryCounts = new int[Categories.Count];
            bool added = false;
            foreach (var item in items)
            {
                if (!Accepts(item) || _itemsByPath.ContainsKey(item.Path)) continue;
                var vm = new MediaItemViewModel(item, OnItemCheckedChanged)
                {
                    AlreadyTransferred = transferredKeys.Contains(item.HistoryKey)
                };
                _allItems.Add(vm);
                _itemsByPath[item.Path] = vm;
                AddToFolderTree(item);
                for (int i = 0; i < Categories.Count; i++)
                {
                    if (Categories[i].Matches(item)) categoryCounts[i]++;
                }
                added = true;
            }

            if (!added) return;
            for (int i = 0; i < Categories.Count; i++) Categories[i].Count += categoryCounts[i];
            NewItemsCount = _allItems.Count(v => !v.AlreadyTransferred);
            OnPropertyChanged(nameof(ItemCount));
        }

        internal void Clear()
        {
            _allItems.Clear();
            _itemsByPath.Clear();
            _storageNodes.Clear();
            FolderRoots.Clear();
            VisibleItems.ReplaceAll(Array.Empty<MediaItemViewModel>());
            _selectionAnchor = null;
            _selectedBytes = 0;
            SelectedCount = 0;
            SelectedTransferredCount = 0;
            NewItemsCount = 0;
            foreach (var category in Categories) category.Count = 0;
            if (SelectedFolder != null || SelectedCategory == null)
            {
                SelectedFolder = null;
                SelectedCategory = Categories[0];
            }
            OnPropertyChanged(nameof(ItemCount));
            UpdateSelectionText();
            UpdateState();
        }

        /// <summary>After a transfer: tick marks become "already on PC" badges.</summary>
        internal void MarkTransferred(IEnumerable<string> paths)
        {
            var done = new List<MediaItemViewModel>();
            foreach (var path in paths)
            {
                if (_itemsByPath.TryGetValue(path, out var vm)) done.Add(vm);
            }
            // Untick first, then flag, so the "already on PC" selection count stays consistent.
            SetChecked(done.Where(v => v.IsChecked).ToList(), false);
            foreach (var vm in done) vm.AlreadyTransferred = true;
            NewItemsCount = _allItems.Count(v => !v.AlreadyTransferred);
            if (TransferFilter != TransferStateFilter.All) RefreshView();
            UpdateState();
        }

        private void AddToFolderTree(MediaItem item)
        {
            if (!_storageNodes.TryGetValue(item.StorageName, out var node))
            {
                node = new TreeNodeViewModel(item.StorageName, item.StorageName, "", null) { IsExpanded = true };
                _storageNodes[item.StorageName] = node;
                FolderRoots.Add(node);
            }

            node.Count++;
            if (item.Folder.Length == 0) return;
            foreach (var segment in item.Folder.Split('/', StringSplitOptions.RemoveEmptyEntries))
            {
                node = node.GetOrAddChild(segment);
                node.Count++;
            }
        }

        // =====================================================================================
        // State from the main view model
        // =====================================================================================

        internal void SetEnvironment(bool ready, bool idle, bool scanning, string? scanError)
        {
            _ready = ready;
            _idle = idle;
            _scanning = scanning;
            _scanError = scanError;
            UpdateState();
        }

        private void UpdateState()
        {
            CanSelect = _ready && VisibleItems.Count > 0;
            CanTransferSelected = _ready && _idle && SelectedCount > 0;
            CanTransferAll = _ready && _idle && _allItems.Count > 0;
            CanImportNew = _ready && _idle && NewItemsCount > 0;
            ImportNewText = NewItemsCount > 0 ? $"Import New ({NewItemsCount:N0})" : "Import New";

            ShowEmptyPanel = _ready && VisibleItems.Count == 0;
            EmptyMessage = _allItems.Count == 0
                ? _scanning ? $"Looking for {ItemNoun}…" : _scanError ?? $"No {ItemNoun} were found on this phone."
                : TransferFilter == TransferStateFilter.NotTransferred && SearchText.Length == 0
                    ? "Everything here is already on this PC."
                    : $"No {ItemNoun} match the current view.";
        }

        // =====================================================================================
        // Browsing: categories, folders, search, sort, filters
        // =====================================================================================

        partial void OnSelectedCategoryChanged(CategoryViewModel? value)
        {
            if (value == null) return;
            if (SelectedFolder != null)
            {
                var folder = SelectedFolder;
                SelectedFolder = null;
                folder.IsSelected = false;
            }
            RefreshView();
        }

        /// <summary>Called by the view when the folder tree selection changes.</summary>
        public void OnFolderSelected(TreeNodeViewModel? node)
        {
            if (node == null)
            {
                if (SelectedCategory == null)
                {
                    SelectedFolder = null;
                    SelectedCategory = Categories[0];
                }
                return;
            }
            SelectedFolder = node;
            SelectedCategory = null;
            RefreshView();
        }

        partial void OnSearchTextChanged(string value) => SearchChanged?.Invoke();

        /// <summary>Raised so the main view model can debounce searching.</summary>
        internal event Action? SearchChanged;

        partial void OnSortModeChanged(SortMode value)
        {
            if (IsMediaLibrary) Settings.SortMode = value;
            else Settings.FilesSortMode = value;
            _settingsManager.Save();
            RefreshView();
        }

        partial void OnTypeFilterChanged(MediaTypeFilter value)
        {
            if (IsMediaLibrary)
            {
                Settings.TypeFilter = value;
                _settingsManager.Save();
            }
            RefreshView();
        }

        partial void OnTransferFilterChanged(TransferStateFilter value) => RefreshView();

        partial void OnIsGridViewChanged(bool value)
        {
            var mode = value ? ViewMode.Grid : ViewMode.List;
            if (IsMediaLibrary) Settings.ViewMode = mode;
            else Settings.FilesViewMode = mode;
            _settingsManager.Save();
        }

        [RelayCommand]
        private void ShowGrid() => IsGridView = true;

        [RelayCommand]
        private void ShowList() => IsGridView = false;

        internal void RefreshView()
        {
            Func<MediaItem, bool>? scope = SelectedFolder != null ? SelectedFolder.Contains : SelectedCategory?.Predicate;
            IEnumerable<MediaItemViewModel> source = TransferFilter switch
            {
                TransferStateFilter.NotTransferred => _allItems.Where(v => !v.AlreadyTransferred),
                TransferStateFilter.Transferred => _allItems.Where(v => v.AlreadyTransferred),
                _ => _allItems
            };
            var items = MediaQuery.Apply(source, v => v.Item, scope, IsMediaLibrary ? TypeFilter : MediaTypeFilter.All, SearchText, SortMode);
            VisibleItems.ReplaceAll(items);

            ViewTitle = (SelectedFolder != null
                ? SelectedFolder.Folder.Length == 0 ? SelectedFolder.Name : SelectedFolder.Folder
                : SelectedCategory?.Name ?? Header).ToUpperInvariant();

            UpdateState();
        }

        // =====================================================================================
        // Selection
        // =====================================================================================

        private void OnItemCheckedChanged(MediaItemViewModel vm, bool isChecked)
        {
            int delta = isChecked ? 1 : -1;
            _selectedBytes += delta * vm.Item.Size;
            SelectedCount += delta;
            if (vm.AlreadyTransferred) SelectedTransferredCount += delta;
            if (!_selectionBatch)
            {
                UpdateSelectionText();
                UpdateState();
                _selectionChanged();
            }
        }

        private void UpdateSelectionText()
        {
            SelectionText = SelectedCount == 0
                ? Kind == LibraryKind.Media
                    ? "No photos selected — tick photos to transfer, or use Transfer All"
                    : "No files selected — tick files to transfer, or use Transfer All"
                : $"{SelectedCount:N0} selected  •  {FileUtilities.FormatBytes(_selectedBytes)}" +
                  (SelectedTransferredCount > 0 ? $"  ({SelectedTransferredCount:N0} already on PC)" : "");
            TransferButtonText = SelectedCount > 0 ? $"TRANSFER {SelectedCount:N0}" : "TRANSFER";
            UntickTransferredText = $"Untick already on PC ({SelectedTransferredCount:N0})";
        }

        private void SetChecked(IEnumerable<MediaItemViewModel> items, bool value)
        {
            _selectionBatch = true;
            try
            {
                foreach (var item in items) item.IsChecked = value;
            }
            finally
            {
                _selectionBatch = false;
                UpdateSelectionText();
                UpdateState();
                _selectionChanged();
            }
        }

        [RelayCommand]
        private void SelectAll() => SetChecked(VisibleItems.ToList(), true);

        [RelayCommand]
        private void ClearSelection() => SetChecked(_allItems.Where(v => v.IsChecked).ToList(), false);

        [RelayCommand]
        private void UntickTransferred() => SetChecked(_allItems.Where(v => v.IsChecked && v.AlreadyTransferred).ToList(), false);

        /// <summary>Click toggles a tile, Shift+click ticks a range, double-click opens the preview.</summary>
        public void OnTileClicked(MediaItemViewModel vm, bool shift, int clickCount)
        {
            if (clickCount >= 2)
            {
                vm.IsChecked = !vm.IsChecked; // undo the toggle from the first click of the double-click
                OpenPreview(vm);
                return;
            }

            if (shift && _selectionAnchor != null)
            {
                int from = VisibleItems.IndexOf(_selectionAnchor);
                int to = VisibleItems.IndexOf(vm);
                if (from >= 0 && to >= 0)
                {
                    if (from > to) (from, to) = (to, from);
                    SetChecked(VisibleItems.Skip(from).Take(to - from + 1).ToList(), true);
                    _selectionAnchor = vm;
                    return;
                }
            }

            vm.IsChecked = !vm.IsChecked;
            _selectionAnchor = vm;
        }

        public void ToggleChecked(IReadOnlyList<MediaItemViewModel> items)
        {
            if (items.Count == 0) return;
            SetChecked(items, !items.All(i => i.IsChecked));
        }

        public void OnTileRealized(MediaItemViewModel vm, object tile) => vm.OnRealized(tile, _thumbnails);

        // =====================================================================================
        // Preview / context menu / transfer
        // =====================================================================================

        public void OpenPreview(MediaItemViewModel vm) => _openPreview(vm, VisibleItems.ToList());

        public void TransferItem(MediaItemViewModel vm) => _startTransfer(new[] { vm });

        public void ShowFolderOf(MediaItemViewModel vm)
        {
            if (!_storageNodes.TryGetValue(vm.Item.StorageName, out var node)) return;
            node.IsExpanded = true;
            if (vm.Item.Folder.Length > 0)
            {
                foreach (var segment in vm.Item.Folder.Split('/', StringSplitOptions.RemoveEmptyEntries))
                {
                    node = node.GetOrAddChild(segment);
                    node.IsExpanded = true;
                }
            }
            node.IsSelected = true; // the TreeView raises SelectedItemChanged → OnFolderSelected
        }

        [RelayCommand]
        private void TransferSelected() => _startTransfer(_allItems.Where(v => v.IsChecked).ToList());

        [RelayCommand]
        private void TransferAll() => _startTransfer(_allItems.ToList());

        [RelayCommand]
        private void ImportNew() => _startTransfer(_allItems.Where(v => !v.AlreadyTransferred).ToList());
    }
}
