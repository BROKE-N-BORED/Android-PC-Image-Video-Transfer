using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Infrastructure;

namespace AndroidPhotoTransfer.Core.Media
{
    internal sealed class MediaScanner : IMediaScanner
    {
        /// <summary>Folders scanned first so the photos people care about appear quickly.</summary>
        private static readonly string[] PriorityFolders = { "dcim", "pictures", "download", "movies", "documents" };

        /// <summary>App-private data: huge, not user media, and blocked on modern Android anyway.</summary>
        private static readonly HashSet<string> SkippedFolders = new(StringComparer.OrdinalIgnoreCase)
        {
            "android/data", "android/obb"
        };

        public async Task ScanAsync(IWpdAdapter device, IReadOnlyList<StorageRoot> roots,
            Action<IReadOnlyList<MediaItem>> onBatch, CancellationToken ct)
        {
            foreach (var root in roots)
            {
                var pending = new Queue<(string Path, string Relative)>();
                pending.Enqueue((root.Path, ""));

                while (pending.Count > 0)
                {
                    ct.ThrowIfCancellationRequested();
                    var (folderPath, relative) = pending.Dequeue();
                    var subfolders = new List<(string Path, string Relative)>();

                    try
                    {
                        await device.ListFolderAsync(folderPath, entries =>
                        {
                            List<MediaItem>? found = null;
                            foreach (var entry in entries)
                            {
                                if (entry.IsFolder)
                                {
                                    var childRelative = relative.Length == 0 ? entry.Name : relative + "/" + entry.Name;
                                    if (!ShouldSkipFolder(entry, childRelative)) subfolders.Add((entry.Path, childRelative));
                                    continue;
                                }

                                // Every visible file is collected: photos/videos feed the Photos tab, everything feeds the Files tab.
                                if (entry.Name.StartsWith('.') || entry.IsHidden) continue;
                                var kind = MediaClassifier.GetKind(entry.Name) ?? MediaKind.File;

                                (found ??= new List<MediaItem>()).Add(new MediaItem(entry.Path, entry.Name, root.Name, relative,
                                    folderPath, entry.Size, entry.Created, entry.Modified, kind, entry.PersistentId));
                            }
                            if (found != null) onBatch(found);
                        }, ct);
                    }
                    catch (OperationCanceledException)
                    {
                        throw;
                    }
                    catch (Exception ex)
                    {
                        if (!await device.IsStillConnectedAsync()) throw new DeviceDisconnectedException(inner: ex);
                        Logger.Warn($"Skipped unreadable folder '{relative}'", ex);
                    }

                    if (relative.Length == 0)
                    {
                        subfolders.Sort((a, b) => Priority(a.Relative).CompareTo(Priority(b.Relative)));
                    }
                    foreach (var subfolder in subfolders) pending.Enqueue(subfolder);
                }
            }
        }

        private static bool ShouldSkipFolder(DeviceEntry folder, string relative) =>
            folder.Name.StartsWith('.') || folder.IsHidden || SkippedFolders.Contains(relative);

        private static int Priority(string rootFolder)
        {
            int index = Array.IndexOf(PriorityFolders, rootFolder.ToLowerInvariant());
            if (index >= 0) return index;
            return rootFolder.Equals("android", StringComparison.OrdinalIgnoreCase) ? int.MaxValue : PriorityFolders.Length;
        }
    }
}
