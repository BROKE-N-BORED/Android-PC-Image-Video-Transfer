using System.IO;

namespace AndroidPhotoTransfer.Core.Media
{
    internal static class MediaClassifier
    {
        private static readonly HashSet<string> PhotoExtensions = new(StringComparer.OrdinalIgnoreCase)
        {
            ".jpg", ".jpeg", ".jpe", ".png", ".heic", ".heif", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".avif",
            ".dng", ".raw", ".arw", ".cr2", ".cr3", ".nef", ".orf", ".rw2"
        };

        private static readonly HashSet<string> VideoExtensions = new(StringComparer.OrdinalIgnoreCase)
        {
            ".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".mkv", ".avi", ".webm", ".wmv", ".mts", ".m2ts", ".ts"
        };

        /// <summary>Formats WPF can decode out of the box (HEIC/WebP/AVIF work when the Windows codec is installed).</summary>
        private static readonly HashSet<string> DecodableExtensions = new(StringComparer.OrdinalIgnoreCase)
        {
            ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".webp", ".avif"
        };

        public static MediaKind? GetKind(string fileName)
        {
            var extension = Path.GetExtension(fileName);
            if (PhotoExtensions.Contains(extension)) return MediaKind.Photo;
            if (VideoExtensions.Contains(extension)) return MediaKind.Video;
            return null;
        }

        public static bool CanDecode(string extension) => DecodableExtensions.Contains(extension);

        private static readonly HashSet<string> DocumentExtensions = new(StringComparer.OrdinalIgnoreCase)
        {
            ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".xls", ".xlsx", ".ods", ".csv", ".ppt", ".pptx",
            ".odp", ".epub", ".html", ".htm", ".json", ".xml", ".vcf", ".ics"
        };

        private static readonly HashSet<string> AudioExtensions = new(StringComparer.OrdinalIgnoreCase)
        {
            ".mp3", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".flac", ".amr", ".wma", ".mid", ".midi", ".3ga"
        };

        private static readonly HashSet<string> ArchiveExtensions = new(StringComparer.OrdinalIgnoreCase)
        {
            ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"
        };

        private static readonly HashSet<string> AppExtensions = new(StringComparer.OrdinalIgnoreCase)
        {
            ".apk", ".apks", ".xapk", ".apkm"
        };

        public static FileCategory GetFileCategory(string fileName, MediaKind kind)
        {
            if (kind == MediaKind.Photo) return FileCategory.Photo;
            if (kind == MediaKind.Video) return FileCategory.Video;
            var extension = Path.GetExtension(fileName);
            if (DocumentExtensions.Contains(extension)) return FileCategory.Document;
            if (AudioExtensions.Contains(extension)) return FileCategory.Audio;
            if (ArchiveExtensions.Contains(extension)) return FileCategory.Archive;
            if (AppExtensions.Contains(extension)) return FileCategory.App;
            return FileCategory.Other;
        }

        /// <summary>Category from the folder relative to the storage root, e.g. "DCIM/Camera".</summary>
        public static MediaCategory GetCategory(string folder)
        {
            var f = folder.Replace('\\', '/').ToLowerInvariant();
            if (f.Contains("screenshot")) return MediaCategory.Screenshots;
            if (f == "dcim" || f.StartsWith("dcim/")) return MediaCategory.Camera;
            if (f == "download" || f.StartsWith("download/") || f == "downloads" || f.StartsWith("downloads/")) return MediaCategory.Downloads;
            if (f == "pictures" || f.StartsWith("pictures/")) return MediaCategory.Pictures;
            return MediaCategory.Other;
        }
    }
}
