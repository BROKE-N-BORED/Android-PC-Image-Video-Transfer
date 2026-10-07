using System.IO;
using System.Runtime.InteropServices;
using System.Text;

namespace AndroidPhotoTransfer.Infrastructure
{
    internal static class FileUtilities
    {
        private static readonly char[] InvalidNameChars = Path.GetInvalidFileNameChars();

        public static string FormatBytes(long bytes)
        {
            if (bytes < 1024) return $"{bytes} B";
            double value = bytes;
            string[] units = { "KB", "MB", "GB", "TB" };
            int unit = -1;
            while (value >= 1024 && unit < units.Length - 1)
            {
                value /= 1024;
                unit++;
            }
            return value >= 100 ? $"{value:F0} {units[unit]}" : $"{value:F1} {units[unit]}";
        }

        /// <summary>Makes a phone file name safe for Windows (phones allow characters like ':' and '?').</summary>
        public static string SanitizeFileName(string name)
        {
            var builder = new StringBuilder(name.Length);
            foreach (var c in name) builder.Append(Array.IndexOf(InvalidNameChars, c) >= 0 ? '_' : c);
            var result = builder.ToString().Trim().TrimEnd('.');
            return string.IsNullOrEmpty(result) ? "unnamed" : result;
        }

        /// <summary>Turns a phone folder such as "DCIM/Camera" into a safe relative Windows path.</summary>
        public static string SanitizeRelativeFolder(string folder)
        {
            var parts = folder.Split(new[] { '/', '\\' }, StringSplitOptions.RemoveEmptyEntries)
                .Where(p => p != "." && p != "..")
                .Select(SanitizeFileName);
            return Path.Combine(parts.ToArray());
        }

        /// <summary>Free bytes available to the user on the volume holding <paramref name="folder"/> (works for UNC paths too).</summary>
        public static long? GetAvailableFreeSpace(string folder)
        {
            try
            {
                var existing = Path.GetFullPath(folder);
                while (!Directory.Exists(existing))
                {
                    var parent = Path.GetDirectoryName(existing);
                    if (parent == null) return null;
                    existing = parent;
                }
                return GetDiskFreeSpaceEx(existing, out var available, out _, out _) ? (long)available : null;
            }
            catch
            {
                return null;
            }
        }

        public static void TryDelete(string path)
        {
            try
            {
                if (File.Exists(path)) File.Delete(path);
            }
            catch (Exception ex)
            {
                Logger.Warn("Could not delete temporary file", ex);
            }
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetDiskFreeSpaceEx(string directoryName, out ulong freeBytesAvailable,
            out ulong totalNumberOfBytes, out ulong totalNumberOfFreeBytes);
    }
}
