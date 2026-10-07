using AndroidPhotoTransfer.Core.Thumbnails;
using AndroidPhotoTransfer.Infrastructure;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace AndroidPhotoTransfer.UI.ViewModels
{
    public sealed partial class SettingsViewModel : ObservableObject
    {
        private readonly SettingsManager _manager;
        private readonly ThumbnailCache _cache;
        private readonly Action _onSaved;

        internal SettingsViewModel(SettingsManager manager, ThumbnailCache cache, Action onSaved)
        {
            _manager = manager;
            _cache = cache;
            _onSaved = onSaved;

            var s = manager.Current;
            _defaultDestination = s.DefaultDestination;
            _rememberDestination = s.RememberDestination;
            _skipExistingFiles = s.SkipExistingFiles;
            _openFolderAfterTransfer = s.OpenFolderAfterTransfer;
            _organizeByPhoneFolder = s.OrganizeByPhoneFolder;
            _generateThumbnails = s.GenerateThumbnails;
            _thumbnailCacheMegabytes = s.ThumbnailCacheMegabytes;
            UpdateCacheUsage();
        }

        public event Action? CloseRequested;

        public IReadOnlyList<int> CacheSizes { get; } = new[] { 32, 64, 128, 256, 512 };

        public IReadOnlyList<Option<bool>> ExistingFileOptions { get; } = new[]
        {
            new Option<bool>(true, "Skip"),
            new Option<bool>(false, "Keep both (add _1, _2…)")
        };

        [ObservableProperty] private string _defaultDestination;
        [ObservableProperty] private bool _rememberDestination;
        [ObservableProperty] private bool _skipExistingFiles;
        [ObservableProperty] private bool _openFolderAfterTransfer;
        [ObservableProperty] private bool _organizeByPhoneFolder;
        [ObservableProperty] private bool _generateThumbnails;
        [ObservableProperty] private int _thumbnailCacheMegabytes;
        [ObservableProperty] private string _cacheUsageText = "";

        public string LogsFolder => AppPaths.LogsFolder;

        private void UpdateCacheUsage() =>
            CacheUsageText = $"{_cache.Count:N0} thumbnails in memory ({FileUtilities.FormatBytes(_cache.CurrentBytes)})";

        [RelayCommand]
        private void Browse()
        {
            var folder = Dialogs.PickFolder(DefaultDestination, "Choose the default destination folder");
            if (folder != null) DefaultDestination = folder;
        }

        [RelayCommand]
        private void ClearThumbnailCache()
        {
            _cache.Clear();
            UpdateCacheUsage();
        }

        [RelayCommand]
        private void OpenLogs() => Dialogs.OpenInExplorer(AppPaths.LogsFolder);

        [RelayCommand]
        private void Save()
        {
            var s = _manager.Current;
            if (!string.Equals(s.DefaultDestination, DefaultDestination, StringComparison.OrdinalIgnoreCase))
            {
                // Changing the default means "use this from now on".
                s.LastDestination = null;
            }
            s.DefaultDestination = DefaultDestination;
            s.RememberDestination = RememberDestination;
            s.SkipExistingFiles = SkipExistingFiles;
            s.OpenFolderAfterTransfer = OpenFolderAfterTransfer;
            s.OrganizeByPhoneFolder = OrganizeByPhoneFolder;
            s.GenerateThumbnails = GenerateThumbnails;
            s.ThumbnailCacheMegabytes = ThumbnailCacheMegabytes;
            _manager.Save();
            _onSaved();
            CloseRequested?.Invoke();
        }

        [RelayCommand]
        private void Cancel() => CloseRequested?.Invoke();
    }
}
