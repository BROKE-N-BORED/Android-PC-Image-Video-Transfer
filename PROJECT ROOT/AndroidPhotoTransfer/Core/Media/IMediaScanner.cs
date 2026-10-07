using AndroidPhotoTransfer.Core.Wpd;

namespace AndroidPhotoTransfer.Core.Media
{
    public interface IMediaScanner
    {
        /// <summary>
        /// Walks the phone's storage and reports every visible file (photos, videos and other files) in batches as they are found,
        /// so the UI can show results long before the scan finishes.
        /// </summary>
        Task ScanAsync(IWpdAdapter device, IReadOnlyList<StorageRoot> roots,
            Action<IReadOnlyList<MediaItem>> onBatch, CancellationToken ct);
    }
}
