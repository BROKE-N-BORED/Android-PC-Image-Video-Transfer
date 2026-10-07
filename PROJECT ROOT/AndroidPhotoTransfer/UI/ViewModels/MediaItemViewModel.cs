using System.Windows.Media.Imaging;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Thumbnails;
using AndroidPhotoTransfer.Infrastructure;
using CommunityToolkit.Mvvm.ComponentModel;

namespace AndroidPhotoTransfer.UI.ViewModels
{
    /// <summary>One tile/row. Holds a thumbnail only while realized on screen; the LRU cache keeps recent ones.</summary>
    public sealed partial class MediaItemViewModel : ObservableObject, IThumbnailTarget
    {
        private readonly Action<MediaItemViewModel, bool> _checkedChanged;
        private volatile bool _isRealized;
        private readonly HashSet<object> _tiles = new(ReferenceEqualityComparer.Instance);
        private volatile bool _thumbnailRequested;

        public MediaItemViewModel(MediaItem item, Action<MediaItemViewModel, bool> checkedChanged)
        {
            Item = item;
            _checkedChanged = checkedChanged;
        }

        public MediaItem Item { get; }

        [ObservableProperty]
        private bool _isChecked;

        private WeakReference<BitmapSource>? _thumbnail;

        /// <summary>
        /// Held weakly: the on-screen Image and the LRU cache keep it alive, so memory stays bounded by the
        /// cache budget no matter how many tiles have been scrolled past.
        /// </summary>
        public BitmapSource? Thumbnail
        {
            get => _thumbnail != null && _thumbnail.TryGetTarget(out var image) ? image : null;
            private set
            {
                _thumbnail = value == null ? null : new WeakReference<BitmapSource>(value);
                OnPropertyChanged();
            }
        }

        [ObservableProperty]
        private bool _thumbnailFailed;

        [ObservableProperty]
        private bool _alreadyTransferred;

        public bool IsRealized => _isRealized;

        public bool ThumbnailRequested
        {
            get => _thumbnailRequested;
            set => _thumbnailRequested = value;
        }

        public string Name => Item.Name;
        public string SizeText => FileUtilities.FormatBytes(Item.Size);
        public string DateText => Item.Date?.ToString("MMM d, yyyy  h:mm tt") ?? "";
        public string FolderText => Item.Folder.Length == 0 ? Item.StorageName : Item.Folder;
        public string KindText => Item.FileCategory switch
        {
            FileCategory.Photo => "Photo",
            FileCategory.Video => "Video",
            FileCategory.Document => "Document",
            FileCategory.Audio => "Audio",
            FileCategory.Archive => "Archive",
            FileCategory.App => "App",
            _ => Item.Extension.Length > 1 ? Item.Extension.TrimStart('.').ToUpperInvariant() + " file" : "File"
        };

        /// <summary>Icon shown when there is no thumbnail.</summary>
        public string TypeGlyph => Item.FileCategory switch
        {
            FileCategory.Photo => "",
            FileCategory.Video => "",
            FileCategory.Document => "",
            FileCategory.Audio => "",
            FileCategory.Archive => "",
            FileCategory.App => "",
            _ => ""
        };
        public bool IsVideo => Item.IsVideo;
        public string ToolTipText => $"{Name}\n{SizeText}  •  {DateText}\n{FolderText}";

        partial void OnIsCheckedChanged(bool value) => _checkedChanged(this, value);

        /// <summary>
        /// Tracks which on-screen tiles show this item. When the grid refreshes, WPF may load the new tile
        /// before unloading the old one, and may raise Loaded more than once, so a flag or counter is unreliable.
        /// </summary>
        internal void OnRealized(object tile, ThumbnailManager thumbnails)
        {
            _tiles.Add(tile);
            _isRealized = true;
            if (Item.IsMedia && Thumbnail == null && !ThumbnailFailed) thumbnails.Request(this);
        }

        public void OnUnrealized(object tile)
        {
            _tiles.Remove(tile);
            if (_tiles.Count > 0) return;
            _isRealized = false;
            Thumbnail = null; // The LRU cache still holds it, within its memory budget.
        }

        public void SetThumbnail(BitmapSource? thumbnail, bool failed)
        {
            if (failed) ThumbnailFailed = true;
            else if (_isRealized) Thumbnail = thumbnail;
        }
    }
}
