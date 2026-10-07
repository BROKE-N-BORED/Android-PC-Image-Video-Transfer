using System.Windows.Media.Imaging;
using System.Windows.Threading;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Infrastructure;

namespace AndroidPhotoTransfer.Core.Thumbnails
{
    /// <summary>Something on screen that wants a thumbnail (a grid tile's view model).</summary>
    public interface IThumbnailTarget
    {
        MediaItem Item { get; }

        /// <summary>True while the tile is realized (on screen or in the small scroll buffer).</summary>
        bool IsRealized { get; }

        bool ThumbnailRequested { get; set; }

        /// <summary>Called on the UI thread.</summary>
        void SetThumbnail(BitmapSource? thumbnail, bool failed);
    }

    /// <summary>
    /// Loads thumbnails one at a time, only for tiles that are still on screen when their turn comes.
    /// Uses the phone's own thumbnails; falls back to decoding the full photo only for small images.
    /// </summary>
    internal sealed class ThumbnailManager
    {
        private const int ThumbnailWidth = 220;
        private const long FullDecodeLimit = 30L * 1024 * 1024;

        private readonly Dispatcher _dispatcher;
        private readonly object _gate = new();
        private readonly List<IThumbnailTarget> _queue = new();
        private IWpdAdapter? _device;
        private int _generation;
        private bool _pumping;
        public ThumbnailManager(ThumbnailCache cache, Dispatcher dispatcher)
        {
            Cache = cache;
            _dispatcher = dispatcher;
        }

        public ThumbnailCache Cache { get; }

        public bool Enabled { get; set; } = true;

        /// <summary>
        /// Asks the UI (on the UI thread) whether a tile is really on screen right now. Checked just before
        /// each fetch so thumbnails for tiles that were filtered or scrolled away never use the phone connection.
        /// </summary>
        public Func<IThumbnailTarget, bool>? IsOnScreen { get; set; }

        private bool _suspended;

        /// <summary>While suspended (e.g. during a transfer) no thumbnails are fetched, so copying gets the full connection.</summary>
        public bool Suspended
        {
            get { lock (_gate) return _suspended; }
            set
            {
                bool start;
                lock (_gate)
                {
                    _suspended = value;
                    start = !value && !_pumping && _queue.Count > 0 && _device != null;
                    if (start) _pumping = true;
                }
                if (start) _ = Task.Run(PumpAsync);
            }
        }

        public void SetDevice(IWpdAdapter? device)
        {
            lock (_gate)
            {
                _device = device;
                _generation++;
                foreach (var pending in _queue) pending.ThumbnailRequested = false;
                _queue.Clear();
            }
            Cache.Clear();
        }

        /// <summary>Call on the UI thread when a tile is realized.</summary>
        public void Request(IThumbnailTarget target)
        {
            if (!Enabled) return;

            var cached = Cache.Get(target.Item.Path);
            if (cached != null)
            {
                target.SetThumbnail(cached, failed: false);
                return;
            }

            lock (_gate)
            {
                if (_device == null || target.ThumbnailRequested) return;
                target.ThumbnailRequested = true;
                _queue.Add(target);
                if (_pumping || _suspended) return;
                _pumping = true;
            }
            _ = Task.Run(PumpAsync);
        }

        private async Task PumpAsync()
        {
            while (true)
            {
                IThumbnailTarget target;
                IWpdAdapter device;
                int generation;
                lock (_gate)
                {
                    if (_queue.Count == 0 || _device == null || _suspended)
                    {
                        _pumping = false;
                        return;
                    }
                    // Newest request first: whatever the user is looking at right now loads first.
                    target = _queue[^1];
                    _queue.RemoveAt(_queue.Count - 1);
                    device = _device;
                    generation = _generation;
                }

                bool onScreen = target.IsRealized;
                var probe = IsOnScreen;
                if (onScreen && probe != null)
                {
                    try
                    {
                        onScreen = await _dispatcher.InvokeAsync(() => probe(target));
                    }
                    catch (TaskCanceledException)
                    {
                        onScreen = false; // app shutting down
                    }
                }

                if (!onScreen)
                {
                    // Scrolled away before its turn: skip it; it will be re-requested if it comes back.
                    target.ThumbnailRequested = false;
                    continue;
                }

                BitmapSource? image = null;
                try
                {
                    image = Cache.Get(target.Item.Path) ?? await LoadAsync(device, target.Item);
                }
                catch (Exception ex)
                {
                    Logger.Warn($"Thumbnail failed for {target.Item.Name}", ex);
                }

                lock (_gate)
                {
                    if (generation != _generation) continue;
                }

                if (image != null) Cache.Add(target.Item.Path, image);                var result = image;
                _ = _dispatcher.BeginInvoke(() =>
                {
                    target.ThumbnailRequested = false;
                    target.SetThumbnail(result, failed: result == null);
                });
            }
        }

        private static async Task<BitmapSource?> LoadAsync(IWpdAdapter device, MediaItem item)
        {
            if (!item.IsMedia) return null;

            var thumbnail = await device.GetThumbnailAsync(item.Path, item.PersistentId, CancellationToken.None);
            if (thumbnail != null)
            {
                var decoded = ImageDecoder.Decode(thumbnail, ThumbnailWidth);
                if (decoded != null) return decoded;
            }

            if (item.Kind == MediaKind.Photo && item.Size > 0 && item.Size <= FullDecodeLimit &&
                MediaClassifier.CanDecode(item.Extension))
            {
                var bytes = await device.ReadAllBytesAsync(item.Path, item.PersistentId, FullDecodeLimit, CancellationToken.None);
                return ImageDecoder.Decode(bytes, ThumbnailWidth);
            }
            return null;
        }
    }
}
