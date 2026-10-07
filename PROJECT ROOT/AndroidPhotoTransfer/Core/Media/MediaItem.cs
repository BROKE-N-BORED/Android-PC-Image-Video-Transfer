namespace AndroidPhotoTransfer.Core.Media
{
    public enum MediaKind { Photo, Video, File }

    /// <summary>Where a photo/video lives on the phone (used by the Photos & Videos tab).</summary>
    public enum MediaCategory { Camera, Screenshots, Pictures, Downloads, Other }

    /// <summary>What kind of file it is (used by the Files tab).</summary>
    public enum FileCategory { Photo, Video, Document, Audio, Archive, App, Other }

    public enum SortMode { NewestFirst, OldestFirst, NameAscending, NameDescending, LargestFirst, SmallestFirst }

    public enum MediaTypeFilter { All, Photos, Videos }

    public enum TransferStateFilter { All, NotTransferred, Transferred }

    /// <summary>Lightweight metadata for one file on the phone. Never holds file contents.</summary>
    public sealed class MediaItem
    {
        public MediaItem(string path, string name, string storageName, string folder, string folderPath,
            long size, DateTime? created, DateTime? modified, MediaKind kind, string? persistentId = null)
        {
            Path = path;
            Name = name;
            StorageName = storageName;
            Folder = folder;
            FolderPath = folderPath;
            Size = size;
            Created = created;
            Modified = modified;
            Kind = kind;
            PersistentId = persistentId;
            Extension = System.IO.Path.GetExtension(name).ToLowerInvariant();
            Category = MediaClassifier.GetCategory(folder);
            FileCategory = MediaClassifier.GetFileCategory(name, kind);
        }

        /// <summary>Device path; also the identity of the item.</summary>
        public string Path { get; }
        public string Name { get; }
        public string Extension { get; }
        public string StorageName { get; }

        /// <summary>Folder relative to its storage root, using '/', e.g. "DCIM/Camera". Empty for the root.</summary>
        public string Folder { get; }

        /// <summary>Device path of the containing folder.</summary>
        public string FolderPath { get; }

        public long Size { get; }
        public DateTime? Created { get; }
        public DateTime? Modified { get; }
        public MediaKind Kind { get; }
        public MediaCategory Category { get; }
        public FileCategory FileCategory { get; }

        /// <summary>
        /// The phone's permanent ID for this file. Opening a file by ID is a single quick request, whereas opening
        /// it by path makes the phone list every file in each folder along the way (very slow in big folders).
        /// </summary>
        public string? PersistentId { get; }

        /// <summary>Best available capture date. Android reports the capture time as the modified time.</summary>
        public DateTime? Date => Modified ?? Created;

        public bool IsVideo => Kind == MediaKind.Video;
        public bool IsMedia => Kind != MediaKind.File;

        /// <summary>Key used by the transfer history to recognise this exact file again.</summary>
        public string HistoryKey => $"{Path}|{Size}";
    }
}
