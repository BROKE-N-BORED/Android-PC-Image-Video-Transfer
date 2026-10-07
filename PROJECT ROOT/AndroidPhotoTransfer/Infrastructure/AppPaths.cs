using System.IO;

namespace AndroidPhotoTransfer.Infrastructure
{
    /// <summary>Well-known locations under %LOCALAPPDATA%\AndroidPhotoTransfer.</summary>
    internal static class AppPaths
    {
        public static string DataFolder { get; } = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AndroidPhotoTransfer");

        public static string SettingsFile => Path.Combine(DataFolder, "settings.json");
        public static string HistoryDatabase => Path.Combine(DataFolder, "history.db");
        public static string LogsFolder => Path.Combine(DataFolder, "logs");

        public static string DefaultDestination { get; } = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.MyPictures), "Phone Photos");
    }
}
