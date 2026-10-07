using System.Windows.Media.Imaging;

namespace AndroidPhotoTransfer.Core.Thumbnails
{
    /// <summary>In-memory LRU cache of decoded thumbnails with a byte budget (default 64 MB).</summary>
    public sealed class ThumbnailCache
    {
        private readonly Dictionary<string, LinkedListNode<Entry>> _map = new();
        private readonly LinkedList<Entry> _lru = new();
        private readonly object _gate = new();
        private long _capacityBytes;
        private long _currentBytes;

        public ThumbnailCache(long capacityBytes)
        {
            _capacityBytes = capacityBytes;
        }

        public long CapacityBytes
        {
            get { lock (_gate) return _capacityBytes; }
            set
            {
                lock (_gate)
                {
                    _capacityBytes = value;
                    Trim(0);
                }
            }
        }

        public long CurrentBytes { get { lock (_gate) return _currentBytes; } }
        public int Count { get { lock (_gate) return _map.Count; } }

        public BitmapSource? Get(string key)
        {
            lock (_gate)
            {
                if (!_map.TryGetValue(key, out var node)) return null;
                _lru.Remove(node);
                _lru.AddFirst(node);
                return node.Value.Image;
            }
        }

        public void Add(string key, BitmapSource image)
        {
            long size = EstimateBytes(image);
            lock (_gate)
            {
                if (_map.TryGetValue(key, out var existing))
                {
                    _lru.Remove(existing);
                    _map.Remove(key);
                    _currentBytes -= existing.Value.Size;
                }
                if (size > _capacityBytes) return;

                Trim(size);
                var node = _lru.AddFirst(new Entry(key, image, size));
                _map[key] = node;
                _currentBytes += size;
            }
        }

        public void Clear()
        {
            lock (_gate)
            {
                _map.Clear();
                _lru.Clear();
                _currentBytes = 0;
            }
        }

        private void Trim(long incoming)
        {
            while (_lru.Last != null && _currentBytes + incoming > _capacityBytes)
            {
                var last = _lru.Last.Value;
                _lru.RemoveLast();
                _map.Remove(last.Key);
                _currentBytes -= last.Size;
            }
        }

        internal static long EstimateBytes(BitmapSource image) =>
            (long)image.PixelWidth * image.PixelHeight * Math.Max(1, (image.Format.BitsPerPixel + 7) / 8);

        private sealed record Entry(string Key, BitmapSource Image, long Size);
    }
}
