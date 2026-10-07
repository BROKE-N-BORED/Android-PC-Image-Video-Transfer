using System.Windows.Media.Imaging;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Thumbnails;
using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Infrastructure;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;

namespace AndroidPhotoTransfer.UI.ViewModels
{
    /// <summary>Double-click preview. Only the photo being previewed is loaded at higher resolution.</summary>
    public sealed partial class PreviewViewModel : ObservableObject
    {
        private const long MaxPreviewBytes = 150L * 1024 * 1024;
        private const int PreviewWidth = 2400;

        private readonly IReadOnlyList<MediaItemViewModel> _items;
        private readonly IWpdAdapter _device;
        private readonly Action<MediaItemViewModel> _transfer;
        private CancellationTokenSource? _loadCts;
        private int _index;

        internal PreviewViewModel(IReadOnlyList<MediaItemViewModel> items, int index, IWpdAdapter device, Action<MediaItemViewModel> transfer)
        {
            _items = items;
            _index = Math.Clamp(index, 0, items.Count - 1);
            _device = device;
            _transfer = transfer;
        }

        public event Action? CloseRequested;

        [ObservableProperty] private BitmapSource? _image;
        [ObservableProperty] private string _title = "";
        [ObservableProperty] private string _details = "";
        [ObservableProperty] private string? _message;
        [ObservableProperty] private bool _isLoading;
        [ObservableProperty] private string _positionText = "";

        public MediaItemViewModel Current => _items[_index];

        public Task LoadAsync() => LoadCurrentAsync();

        private async Task LoadCurrentAsync()
        {
            _loadCts?.Cancel();
            var cts = _loadCts = new CancellationTokenSource();
            var vm = Current;
            var item = vm.Item;

            Title = item.Name;
            PositionText = $"{_index + 1:N0} of {_items.Count:N0}";
            Details = BuildDetails(item, null, null);
            Image = vm.Thumbnail;
            Message = null;
            OnPropertyChanged(nameof(Current));

            if (item.IsVideo)
            {
                Message = "Video preview isn't available. Transfer the video to play it on this PC.";
                return;
            }
            if (!MediaClassifier.CanDecode(item.Extension))
            {
                var type = item.Extension.Length > 1 ? item.Extension.ToUpperInvariant().TrimStart('.') + " files" : "this file";
                Message = $"Preview isn't available for {type}. Transfer it to open it on this PC.";
                return;
            }
            if (item.Size > MaxPreviewBytes)
            {
                Message = "This file is too large to preview. Transfer it to open it.";
                return;
            }

            IsLoading = true;
            try
            {
                var bytes = await _device.ReadAllBytesAsync(item.Path, item.PersistentId, MaxPreviewBytes, cts.Token);
                var (decoded, width, height) = await Task.Run(() =>
                {
                    var image = ImageDecoder.Decode(bytes, PreviewWidth);
                    var size = ImageDecoder.ReadPixelSize(bytes);
                    return (image, size.Width, size.Height);
                }, cts.Token);

                if (cts.IsCancellationRequested) return;
                if (decoded == null)
                {
                    Message = "Windows can't display this image. If it's a HEIC photo, install \"HEIF Image Extensions\" from the Microsoft Store.";
                    return;
                }
                Image = decoded;
                Details = BuildDetails(item, width, height);
            }
            catch (OperationCanceledException)
            {
                // Moved to another photo.
            }
            catch (Exception ex)
            {
                if (!cts.IsCancellationRequested)
                {
                    Logger.Warn($"Preview failed for {item.Name}", ex);
                    Message = ErrorMessages.Describe(ex);
                }
            }
            finally
            {
                if (_loadCts == cts) IsLoading = false;
            }
        }

        private static string BuildDetails(MediaItem item, int? width, int? height)
        {
            var parts = new List<string>();
            if (width > 0 && height > 0) parts.Add($"{width} × {height}");
            parts.Add(FileUtilities.FormatBytes(item.Size));
            if (item.Date.HasValue) parts.Add(item.Date.Value.ToString("MMMM d, yyyy  h:mm tt"));
            parts.Add(item.Folder.Length == 0 ? item.StorageName : item.Folder);
            return string.Join("     •     ", parts);
        }

        [RelayCommand]
        private Task Next()
        {
            if (_index >= _items.Count - 1) return Task.CompletedTask;
            _index++;
            return LoadCurrentAsync();
        }

        [RelayCommand]
        private Task Previous()
        {
            if (_index <= 0) return Task.CompletedTask;
            _index--;
            return LoadCurrentAsync();
        }

        [RelayCommand]
        private void Transfer()
        {
            _transfer(Current);
            Close();
        }

        [RelayCommand]
        private void Close()
        {
            _loadCts?.Cancel();
            CloseRequested?.Invoke();
        }
    }
}
