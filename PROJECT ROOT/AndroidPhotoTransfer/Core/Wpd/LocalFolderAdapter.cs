using System.IO;

namespace AndroidPhotoTransfer.Core.Wpd
{
    /// <summary>
    /// Developer/demo stand-in for a phone: a normal Windows folder treated as the phone's storage.
    /// Only used when the APT_DEMO_FOLDER environment variable is set, so the UI can be tried without a phone.
    /// </summary>
    internal sealed class LocalFolderAdapter : IWpdAdapter
    {
        public const string DeviceIdPrefix = "demo:";
        private readonly string _root;

        public LocalFolderAdapter(string root)
        {
            _root = Path.GetFullPath(root);
            Info = new PhoneInfo(DeviceIdPrefix + _root, "Demo Phone (folder)", "DEMO:" + _root);
        }

        public PhoneInfo Info { get; }
        public bool IsConnected => Directory.Exists(_root);

        public Task<IReadOnlyList<StorageRoot>> GetStorageRootsAsync(CancellationToken ct) =>
            Task.FromResult<IReadOnlyList<StorageRoot>>(new[] { new StorageRoot(_root, "Internal storage", 0, 0) });

        public Task<bool> FolderHasEntriesAsync(string path, CancellationToken ct) =>
            Task.FromResult(Directory.EnumerateFileSystemEntries(path).Any());

        public Task ListFolderAsync(string path, Action<IReadOnlyList<DeviceEntry>> onBatch, CancellationToken ct) =>
            Task.Run(() =>
            {
                var entries = new DirectoryInfo(path).EnumerateFileSystemInfos().Select(info => new DeviceEntry(
                    info.FullName, info.Name, info is DirectoryInfo, info.Attributes.HasFlag(FileAttributes.Hidden),
                    info is FileInfo file ? file.Length : 0, info.CreationTime, info.LastWriteTime,
                    info is FileInfo ? info.FullName : null)).ToList();
                if (entries.Count > 0) onBatch(entries);
            }, ct);

        public Task<Stream> OpenReadAsync(string path, string? persistentId, CancellationToken ct) =>
            Task.FromResult<Stream>(File.OpenRead(persistentId ?? path));

        public Task<byte[]?> GetThumbnailAsync(string path, string? persistentId, CancellationToken ct) =>
            Task.FromResult<byte[]?>(null); // the thumbnail loader falls back to decoding the image itself

        public Task<byte[]> ReadAllBytesAsync(string path, string? persistentId, long maxBytes, CancellationToken ct) =>
            File.ReadAllBytesAsync(persistentId ?? path, ct);

        public Task DeleteFileAsync(string path, CancellationToken ct) => Task.Run(() => File.Delete(path), ct);

        public Task<bool> IsStillConnectedAsync() => Task.FromResult(IsConnected);

        public void Dispose() { }
    }
}
