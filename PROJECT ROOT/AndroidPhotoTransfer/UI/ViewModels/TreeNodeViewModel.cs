using System.Collections.ObjectModel;
using AndroidPhotoTransfer.Core.Media;
using CommunityToolkit.Mvvm.ComponentModel;

namespace AndroidPhotoTransfer.UI.ViewModels
{
    /// <summary>
    /// A folder in the sidebar tree. The tree is built from scanned media paths, so it only shows
    /// folders that actually contain photos/videos and costs no extra phone calls.
    /// </summary>
    public sealed partial class TreeNodeViewModel : ObservableObject
    {
        private readonly Dictionary<string, TreeNodeViewModel> _children = new(StringComparer.OrdinalIgnoreCase);

        public TreeNodeViewModel(string name, string storageName, string folder, TreeNodeViewModel? parent)
        {
            Name = name;
            StorageName = storageName;
            Folder = folder;
            Parent = parent;
        }

        public string Name { get; }
        public string StorageName { get; }

        /// <summary>Folder relative to the storage root ("" for the storage node itself).</summary>
        public string Folder { get; }

        public TreeNodeViewModel? Parent { get; }
        public bool IsStorage => Parent == null;
        public string Glyph => IsStorage ? "" : "";

        public ObservableCollection<TreeNodeViewModel> Children { get; } = new();

        [ObservableProperty]
        private int _count;

        [ObservableProperty]
        private bool _isExpanded;

        [ObservableProperty]
        private bool _isSelected;

        public bool Contains(MediaItem item) =>
            item.StorageName == StorageName &&
            (Folder.Length == 0 || item.Folder.Equals(Folder, StringComparison.OrdinalIgnoreCase) ||
             item.Folder.StartsWith(Folder + "/", StringComparison.OrdinalIgnoreCase));

        public TreeNodeViewModel GetOrAddChild(string name)
        {
            if (_children.TryGetValue(name, out var existing)) return existing;

            var child = new TreeNodeViewModel(name, StorageName, Folder.Length == 0 ? name : Folder + "/" + name, this);
            _children[name] = child;

            int index = 0;
            while (index < Children.Count && StringComparer.OrdinalIgnoreCase.Compare(Children[index].Name, name) < 0) index++;
            Children.Insert(index, child);
            return child;
        }
    }

    public sealed partial class CategoryViewModel : ObservableObject
    {
        public CategoryViewModel(string name, string glyph, Func<MediaItem, bool>? predicate)
        {
            Name = name;
            Glyph = glyph;
            Predicate = predicate;
        }

        public string Name { get; }
        public string Glyph { get; }

        /// <summary>Null means "everything".</summary>
        public Func<MediaItem, bool>? Predicate { get; }

        [ObservableProperty]
        private int _count;

        public bool Matches(MediaItem item) => Predicate == null || Predicate(item);
    }
}
