using System.IO;
using AndroidPhotoTransfer.Infrastructure;

namespace AndroidPhotoTransfer.Core.Transfer
{
    /// <summary>
    /// Picks where a file goes on the PC. Never overwrites: a different file with the same name
    /// becomes IMG_1001_1.jpg, IMG_1001_2.jpg, ... A file with the same name and exact size is treated
    /// as already transferred (skipped) when <c>skipExisting</c> is on.
    /// </summary>
    internal static class DestinationResolver
    {
        public static (string Path, bool IsDuplicate) Resolve(string folder, string fileName, long size, bool skipExisting)
        {
            var safeName = FileUtilities.SanitizeFileName(fileName);
            var stem = Path.GetFileNameWithoutExtension(safeName);
            var extension = Path.GetExtension(safeName);

            for (int i = 0; ; i++)
            {
                var candidate = Path.Combine(folder, i == 0 ? safeName : $"{stem}_{i}{extension}");
                var existing = new FileInfo(candidate);
                if (!existing.Exists) return (candidate, false);
                if (skipExisting && existing.Length == size) return (candidate, true);
            }
        }

        /// <summary>Cheap check used to estimate the space a transfer really needs.</summary>
        public static bool AlreadyExists(string folder, string fileName, long size)
        {
            var existing = new FileInfo(Path.Combine(folder, FileUtilities.SanitizeFileName(fileName)));
            return existing.Exists && existing.Length == size;
        }
    }
}
