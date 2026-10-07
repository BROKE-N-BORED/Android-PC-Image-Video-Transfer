using System.IO;
using System.Text.Json;
using System.Text.Json.Serialization;
using AndroidPhotoTransfer.Core.Media;

namespace AndroidPhotoTransfer.Infrastructure
{
    public enum ViewMode { Grid, List }

    /// <summary>User preferences persisted to settings.json.</summary>
    public sealed class AppSettings
    {
        public string DefaultDestination { get; set; } = AppPaths.DefaultDestination;
        public string? LastDestination { get; set; }
        public bool RememberDestination { get; set; } = true;
        public bool SkipExistingFiles { get; set; } = true;
        public bool OpenFolderAfterTransfer { get; set; } = true;
        public bool OrganizeByPhoneFolder { get; set; }
        public bool GenerateThumbnails { get; set; } = true;
        public int ThumbnailCacheMegabytes { get; set; } = 64;

        public ViewMode ViewMode { get; set; } = ViewMode.Grid;
        public SortMode SortMode { get; set; } = SortMode.NewestFirst;
        public MediaTypeFilter TypeFilter { get; set; } = MediaTypeFilter.All;

        public ViewMode FilesViewMode { get; set; } = ViewMode.List;
        public SortMode FilesSortMode { get; set; } = SortMode.NewestFirst;

        /// <summary>0 = Photos & Videos tab, 1 = Files tab.</summary>
        public int SelectedTab { get; set; }

        public bool FirstRunCompleted { get; set; }

        public double? WindowLeft { get; set; }
        public double? WindowTop { get; set; }
        public double? WindowWidth { get; set; }
        public double? WindowHeight { get; set; }
        public bool WindowMaximized { get; set; }

        /// <summary>The folder transfers go to right now.</summary>
        public string EffectiveDestination =>
            RememberDestination && !string.IsNullOrWhiteSpace(LastDestination) ? LastDestination! : DefaultDestination;
    }

    public sealed class SettingsManager
    {
        private static readonly JsonSerializerOptions JsonOptions = new()
        {
            WriteIndented = true,
            Converters = { new JsonStringEnumConverter() }
        };

        private readonly string _path;

        public SettingsManager(string path)
        {
            _path = path;
            Current = Load();
        }

        public AppSettings Current { get; }

        private AppSettings Load()
        {
            try
            {
                if (File.Exists(_path))
                {
                    var settings = JsonSerializer.Deserialize<AppSettings>(File.ReadAllText(_path), JsonOptions);
                    if (settings != null)
                    {
                        settings.ThumbnailCacheMegabytes = Math.Clamp(settings.ThumbnailCacheMegabytes, 16, 1024);
                        return settings;
                    }
                }
            }
            catch (Exception ex)
            {
                Logger.Warn("Settings file could not be read; using defaults", ex);
            }
            return new AppSettings();
        }

        public void Save()
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(_path)!);
                var temp = _path + ".tmp";
                File.WriteAllText(temp, JsonSerializer.Serialize(Current, JsonOptions));
                File.Move(temp, _path, overwrite: true);
            }
            catch (Exception ex)
            {
                Logger.Warn("Settings could not be saved", ex);
            }
        }
    }
}
