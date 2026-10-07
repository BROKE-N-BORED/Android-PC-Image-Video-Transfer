using System.IO;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Wpd;

namespace AndroidPhotoTransfer.Tests;

/// <summary>An in-memory phone used by the tests in place of a real MTP device.</summary>
internal sealed class FakePhone : IWpdAdapter
{
    public const string Root = @"\Internal shared storage";

    private readonly Dictionary<string, List<DeviceEntry>> _folders = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, byte[]> _files = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, string> _pathByPersistentId = new(StringComparer.Ordinal);
    private int _nextId = 1;

    public PhoneInfo Info { get; } = new("fake-device-id", "Fake Phone", "SN:FAKE");
    public bool Connected { get; set; } = true;
    public bool IsConnected => Connected;

    /// <summary>Called before every chunk read with (path, position); may throw to simulate failures.</summary>
    public Action<string, long>? BeforeRead { get; set; }

    /// <summary>Paths whose stream returns fewer bytes than the reported size.</summary>
    public HashSet<string> Truncated { get; } = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>How each open was addressed: "id" or "path".</summary>
    public List<string> OpenedBy { get; } = new();

    public FakePhone()
    {
        _folders[Root] = new List<DeviceEntry>();
    }

    public MediaItem AddFile(string relativePath, byte[] content, DateTime? modified = null)
    {
        var parts = relativePath.Split('/');
        var folderPath = Root;
        for (int i = 0; i < parts.Length - 1; i++)
        {
            var child = folderPath + "\\" + parts[i];
            if (!_folders.ContainsKey(child))
            {
                _folders[child] = new List<DeviceEntry>();
                _folders[folderPath].Add(new DeviceEntry(child, parts[i], true, false, 0, null, null));
            }
            folderPath = child;
        }

        var name = parts[^1];
        var path = folderPath + "\\" + name;
        var date = modified ?? new DateTime(2026, 10, 1, 12, 0, 0);
        var id = $"PUID-{_nextId++}";
        _files[path] = content;
        _pathByPersistentId[id] = path;
        _folders[folderPath].Add(new DeviceEntry(path, name, false, false, content.Length, date, date, id));

        var folder = string.Join('/', parts.Take(parts.Length - 1));
        return new MediaItem(path, name, "Internal shared storage", folder, folderPath, content.Length, date, date,
            MediaClassifier.GetKind(name) ?? MediaKind.File, id);
    }

    public Task<IReadOnlyList<StorageRoot>> GetStorageRootsAsync(CancellationToken ct) =>
        Task.FromResult<IReadOnlyList<StorageRoot>>(new[] { new StorageRoot(Root, "Internal shared storage", 0, 0) });

    public Task<bool> FolderHasEntriesAsync(string path, CancellationToken ct) =>
        Task.FromResult(_folders.TryGetValue(path, out var entries) && entries.Count > 0);

    public Task ListFolderAsync(string path, Action<IReadOnlyList<DeviceEntry>> onBatch, CancellationToken ct)
    {
        ThrowIfDisconnected();
        if (_folders.TryGetValue(path, out var entries) && entries.Count > 0) onBatch(entries.ToList());
        return Task.CompletedTask;
    }

    public Task<Stream> OpenReadAsync(string path, string? persistentId, CancellationToken ct)
    {
        ThrowIfDisconnected();
        string resolved;
        if (persistentId != null && _pathByPersistentId.TryGetValue(persistentId, out var byId))
        {
            resolved = byId;
            OpenedBy.Add("id");
        }
        else
        {
            resolved = path;
            OpenedBy.Add("path");
        }

        var content = _files[resolved];
        if (Truncated.Contains(resolved)) content = content.Take(content.Length / 2).ToArray();
        return Task.FromResult<Stream>(new FakeStream(this, resolved, content));
    }

    public Task<byte[]?> GetThumbnailAsync(string path, string? persistentId, CancellationToken ct) => Task.FromResult<byte[]?>(null);

    public Task<byte[]> ReadAllBytesAsync(string path, string? persistentId, long maxBytes, CancellationToken ct) =>
        Task.FromResult(_files[path]);

    public Task DeleteFileAsync(string path, CancellationToken ct)
    {
        ThrowIfDisconnected();
        _files.Remove(path);
        return Task.CompletedTask;
    }

    public Task<bool> IsStillConnectedAsync() => Task.FromResult(Connected);

    public void Dispose() { }

    private void ThrowIfDisconnected()
    {
        if (!Connected) throw new IOException("Device not connected (simulated)");
    }

    private sealed class FakeStream : MemoryStream
    {
        private readonly FakePhone _phone;
        private readonly string _path;

        public FakeStream(FakePhone phone, string path, byte[] content) : base(content, writable: false)
        {
            _phone = phone;
            _path = path;
        }

        public override int Read(byte[] buffer, int offset, int count)
        {
            _phone.ThrowIfDisconnected();
            _phone.BeforeRead?.Invoke(_path, Position);
            return base.Read(buffer, offset, Math.Min(count, 64 * 1024)); // MTP returns data in smaller chunks
        }

        public override Task<int> ReadAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return Task.FromResult(Read(buffer, offset, count));
        }

        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
        {
            var temp = new byte[buffer.Length];
            int read = Read(temp, 0, temp.Length);
            temp.AsSpan(0, read).CopyTo(buffer.Span);
            return ValueTask.FromResult(read);
        }
    }
}

/// <summary>Creates and cleans up a temporary destination folder.</summary>
internal sealed class TempFolder : IDisposable
{
    public TempFolder()
    {
        Path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "apt-tests", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path);
    }

    public string Path { get; }

    public void Dispose()
    {
        try { Directory.Delete(Path, recursive: true); } catch { /* best effort */ }
    }
}

internal static class TestData
{
    public static byte[] Bytes(int length, int seed = 1)
    {
        var data = new byte[length];
        new Random(seed).NextBytes(data);
        return data;
    }
}
