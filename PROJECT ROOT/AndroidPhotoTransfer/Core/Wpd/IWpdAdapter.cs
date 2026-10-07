using System.IO;

namespace AndroidPhotoTransfer.Core.Wpd
{
    /// <summary>A phone visible to Windows (not necessarily connected yet).</summary>
    public sealed record PhoneListing(string DeviceId, string Name);

    /// <summary>Identity of a connected phone. <see cref="Key"/> is stable across reconnects and USB ports.</summary>
    public sealed record PhoneInfo(string DeviceId, string Name, string Key);

    public sealed record StorageRoot(string Path, string Name, long TotalBytes, long FreeBytes);

    /// <summary>A file or folder on the phone. <see cref="Path"/> is the device path, e.g. \Internal shared storage\DCIM\Camera\IMG_1.jpg.</summary>
    public sealed record DeviceEntry(string Path, string Name, bool IsFolder, bool IsHidden, long Size, DateTime? Created,
        DateTime? Modified, string? PersistentId = null);

    /// <summary>
    /// All communication with one connected portable device. Implementations must keep device
    /// calls off the UI thread. This is the seam that lets the rest of the app be tested with a fake phone.
    /// Files are addressed by device path plus, when known, the phone's permanent ID (much faster to open).
    /// </summary>
    public interface IWpdAdapter : IDisposable
    {
        PhoneInfo Info { get; }

        /// <summary>False once the connection has died (phone unplugged, USB mode changed, or the phone reset the session).</summary>
        bool IsConnected { get; }

        Task<IReadOnlyList<StorageRoot>> GetStorageRootsAsync(CancellationToken ct);

        /// <summary>True when the folder lists at least one entry (locked phones expose empty storage).</summary>
        Task<bool> FolderHasEntriesAsync(string path, CancellationToken ct);

        /// <summary>Lists a folder in small batches so other device work (thumbnails) can run in between.</summary>
        Task ListFolderAsync(string path, Action<IReadOnlyList<DeviceEntry>> onBatch, CancellationToken ct);

        /// <summary>Opens a streaming read of a file. Reads happen in chunks; the file is never fully loaded into memory.</summary>
        Task<Stream> OpenReadAsync(string path, string? persistentId, CancellationToken ct);

        /// <summary>The device-generated thumbnail, or null if the device has none.</summary>
        Task<byte[]?> GetThumbnailAsync(string path, string? persistentId, CancellationToken ct);

        /// <summary>Reads a whole (small) file, used for previews. Throws if the file is larger than <paramref name="maxBytes"/>.</summary>
        Task<byte[]> ReadAllBytesAsync(string path, string? persistentId, long maxBytes, CancellationToken ct);

        /// <summary>Whether the connection is alive and Windows still lists this device.</summary>
        Task<bool> IsStillConnectedAsync();
    }
}
