using System.IO;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Transfer;
using AndroidPhotoTransfer.Data;

namespace AndroidPhotoTransfer.Tests;

public class TransferManagerTests
{
    private sealed class MemoryHistory : IHistoryDatabase
    {
        public List<(string Device, string Key, string Destination)> Records { get; } = new();
        public void Record(string deviceKey, MediaItem item, string destination) => Records.Add((deviceKey, item.HistoryKey, destination));
        public HashSet<string> GetTransferredKeys(string deviceKey) => Records.Where(r => r.Device == deviceKey).Select(r => r.Key).ToHashSet();
    }

    private static Task<TransferSummary> Run(FakePhone phone, IEnumerable<TransferJob> jobs, string destination,
        MemoryHistory? history = null, bool skipExisting = true, bool organize = false, CancellationToken ct = default) =>
        new TransferManager(history).RunAsync(phone, jobs.ToList(), new TransferOptions(destination, skipExisting, organize),
            new TransferProgress(), new PauseGate(), ct);

    [Fact]
    public async Task Copies_every_file_byte_for_byte_and_leaves_no_partial_files()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var small = TestData.Bytes(1000, 1);
        var large = TestData.Bytes(3 * 1024 * 1024 + 17, 2); // spans several 1 MB chunks
        var a = phone.AddFile("DCIM/Camera/IMG_0001.jpg", small);
        var b = phone.AddFile("DCIM/Camera/VID_0002.mp4", large);
        var history = new MemoryHistory();

        var jobs = new[] { new TransferJob(a), new TransferJob(b) };
        var summary = await Run(phone, jobs, dest.Path, history);

        Assert.Equal(2, summary.Copied);
        Assert.Equal(0, summary.Failed);
        Assert.Equal(small, File.ReadAllBytes(Path.Combine(dest.Path, "IMG_0001.jpg")));
        Assert.Equal(large, File.ReadAllBytes(Path.Combine(dest.Path, "VID_0002.mp4")));
        Assert.Empty(Directory.GetFiles(dest.Path, "*.partial"));
        Assert.All(jobs, j => Assert.Equal(TransferState.Complete, j.State));
        Assert.Equal(2, history.Records.Count);
        Assert.Equal(new DateTime(2026, 10, 1, 12, 0, 0), File.GetLastWriteTime(Path.Combine(dest.Path, "IMG_0001.jpg")));
    }

    [Fact]
    public async Task Skips_identical_existing_file_and_never_overwrites_a_different_one()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var same = phone.AddFile("DCIM/Camera/IMG_1.jpg", TestData.Bytes(500, 3));
        var different = phone.AddFile("DCIM/Camera/IMG_2.jpg", TestData.Bytes(800, 4));

        File.WriteAllBytes(Path.Combine(dest.Path, "IMG_1.jpg"), TestData.Bytes(500, 3));    // already transferred
        var unrelated = TestData.Bytes(123, 99);
        File.WriteAllBytes(Path.Combine(dest.Path, "IMG_2.jpg"), unrelated);                // a different photo, same name

        var jobs = new[] { new TransferJob(same), new TransferJob(different) };
        var summary = await Run(phone, jobs, dest.Path);

        Assert.Equal(1, summary.Skipped);
        Assert.Equal(1, summary.Copied);
        Assert.Equal(unrelated, File.ReadAllBytes(Path.Combine(dest.Path, "IMG_2.jpg")));   // untouched
        Assert.True(File.Exists(Path.Combine(dest.Path, "IMG_2_1.jpg")));                    // saved alongside
        Assert.Equal(Path.Combine(dest.Path, "IMG_2_1.jpg"), jobs[1].DestinationPath);
    }

    [Fact]
    public async Task Keep_both_mode_renames_even_identical_files()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var item = phone.AddFile("DCIM/IMG.jpg", TestData.Bytes(300, 5));
        File.WriteAllBytes(Path.Combine(dest.Path, "IMG.jpg"), TestData.Bytes(300, 5));

        var summary = await Run(phone, new[] { new TransferJob(item) }, dest.Path, skipExisting: false);

        Assert.Equal(1, summary.Copied);
        Assert.True(File.Exists(Path.Combine(dest.Path, "IMG_1.jpg")));
    }

    [Fact]
    public async Task Incomplete_copy_is_failed_and_discarded()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var item = phone.AddFile("DCIM/Camera/IMG_9.jpg", TestData.Bytes(10_000, 6));
        phone.Truncated.Add(item.Path);

        var job = new TransferJob(item);
        var summary = await Run(phone, new[] { job }, dest.Path);

        Assert.Equal(1, summary.Failed);
        Assert.Equal(TransferState.Failed, job.State);
        Assert.NotNull(job.Error);
        Assert.Empty(Directory.GetFiles(dest.Path));
    }

    [Fact]
    public async Task Transient_read_error_is_retried_once()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var item = phone.AddFile("DCIM/Camera/IMG_7.jpg", TestData.Bytes(200_000, 7));
        int failures = 0;
        phone.BeforeRead = (_, position) =>
        {
            if (position > 0 && failures == 0)
            {
                failures++;
                throw new IOException("Simulated glitch");
            }
        };

        var summary = await Run(phone, new[] { new TransferJob(item) }, dest.Path);

        Assert.Equal(1, summary.Copied);
        Assert.Equal(TestData.Bytes(200_000, 7), File.ReadAllBytes(Path.Combine(dest.Path, "IMG_7.jpg")));
    }

    [Fact]
    public async Task Cancel_mid_file_removes_partial_and_marks_remaining_cancelled()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var first = phone.AddFile("DCIM/Camera/A.jpg", TestData.Bytes(1000, 8));
        var second = phone.AddFile("DCIM/Camera/B.mp4", TestData.Bytes(2 * 1024 * 1024, 9));
        var third = phone.AddFile("DCIM/Camera/C.jpg", TestData.Bytes(1000, 10));
        using var cts = new CancellationTokenSource();
        phone.BeforeRead = (path, position) =>
        {
            if (path == second.Path && position > 256 * 1024) cts.Cancel();
        };

        var jobs = new[] { new TransferJob(first), new TransferJob(second), new TransferJob(third) };
        var summary = await Run(phone, jobs, dest.Path, ct: cts.Token);

        Assert.Equal(TransferState.Complete, jobs[0].State);
        Assert.Equal(TransferState.Cancelled, jobs[1].State);
        Assert.Equal(TransferState.Cancelled, jobs[2].State);
        Assert.Equal(1, summary.Copied);
        Assert.Equal(new[] { "A.jpg" }, Directory.GetFiles(dest.Path).Select(Path.GetFileName));
    }

    [Fact]
    public async Task Phone_unplugged_keeps_remaining_jobs_and_resume_finishes_them()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var items = Enumerable.Range(1, 4).Select(i => phone.AddFile($"DCIM/Camera/IMG_{i}.jpg", TestData.Bytes(150_000, i))).ToList();
        phone.BeforeRead = (path, position) =>
        {
            if (path == items[2].Path && position > 0) phone.Connected = false; // unplugged mid-file
        };

        var jobs = items.Select(i => new TransferJob(i)).ToList();
        var first = await Run(phone, jobs, dest.Path);

        Assert.True(first.DeviceDisconnected);
        Assert.Equal(2, first.Copied);
        Assert.Equal(2, first.Remaining);
        Assert.Equal(TransferState.Pending, jobs[2].State);
        Assert.Empty(Directory.GetFiles(dest.Path, "*.partial"));
        Assert.False(File.Exists(Path.Combine(dest.Path, "IMG_3.jpg")));

        // Plug it back in and resume the same jobs.
        phone.Connected = true;
        phone.BeforeRead = null;
        var second = await Run(phone, jobs, dest.Path);

        Assert.Equal(2, second.Copied);
        Assert.All(jobs, j => Assert.Equal(TransferState.Complete, j.State));
        Assert.Equal(4, Directory.GetFiles(dest.Path).Length);
    }

    [Fact]
    public async Task Organize_by_folder_mirrors_the_phone_folders()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var camera = phone.AddFile("DCIM/Camera/IMG_1.jpg", TestData.Bytes(100, 11));
        var shot = phone.AddFile("Pictures/Screenshots/Screenshot_1.png", TestData.Bytes(100, 12));

        await Run(phone, new[] { new TransferJob(camera), new TransferJob(shot) }, dest.Path, organize: true);

        Assert.True(File.Exists(Path.Combine(dest.Path, "DCIM", "Camera", "IMG_1.jpg")));
        Assert.True(File.Exists(Path.Combine(dest.Path, "Pictures", "Screenshots", "Screenshot_1.png")));
    }

    [Fact]
    public async Task Pause_holds_the_transfer_until_resumed()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var item = phone.AddFile("DCIM/IMG.jpg", TestData.Bytes(1000, 13));
        var pause = new PauseGate();
        pause.Pause();

        var run = new TransferManager(null).RunAsync(phone, new[] { new TransferJob(item) },
            new TransferOptions(dest.Path, true, false), new TransferProgress(), pause, CancellationToken.None);

        await Task.Delay(300);
        Assert.False(run.IsCompleted);
        Assert.Empty(Directory.GetFiles(dest.Path));

        pause.Resume();
        var summary = await run.WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Equal(1, summary.Copied);
    }

    [Fact]
    public async Task Files_are_opened_by_their_phone_id_not_by_path()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var items = Enumerable.Range(1, 3).Select(i => phone.AddFile($"DCIM/Camera/IMG_{i}.jpg", TestData.Bytes(1000, i))).ToList();

        await Run(phone, items.Select(i => new TransferJob(i)), dest.Path);

        Assert.Equal(new[] { "id", "id", "id" }, phone.OpenedBy);
    }

    [Fact]
    public async Task Pausing_mid_file_discards_it_and_restarts_it_cleanly_on_resume()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var content = TestData.Bytes(3 * 1024 * 1024, 21);
        var item = phone.AddFile("DCIM/Camera/VID_big.mp4", content);
        var pause = new PauseGate();
        var pausedMidFile = new TaskCompletionSource();
        phone.BeforeRead = (_, position) =>
        {
            if (position > 512 * 1024 && !pausedMidFile.Task.IsCompleted)
            {
                pause.Pause();
                pausedMidFile.TrySetResult();
            }
        };

        var progress = new TransferProgress();
        var run = new TransferManager(null).RunAsync(phone, new[] { new TransferJob(item) },
            new TransferOptions(dest.Path, true, false), progress, pause, CancellationToken.None);

        await pausedMidFile.Task.WaitAsync(TimeSpan.FromSeconds(10));
        await Task.Delay(300);
        Assert.False(run.IsCompleted);
        Assert.Empty(Directory.GetFiles(dest.Path)); // half-copied file was removed, nothing left open

        pause.Resume();
        var summary = await run.WaitAsync(TimeSpan.FromSeconds(10));

        Assert.Equal(1, summary.Copied);
        Assert.Equal(content, File.ReadAllBytes(Path.Combine(dest.Path, "VID_big.mp4")));
        Assert.Equal(content.Length, progress.Snapshot().BytesCopied); // restarted bytes not double counted
    }

    [Fact]
    public async Task Repeated_failures_stop_the_transfer_and_keep_the_rest_for_resume()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var items = Enumerable.Range(1, 20).Select(i => phone.AddFile($"DCIM/Camera/IMG_{i}.jpg", TestData.Bytes(5000, i))).ToList();
        var good = items.Take(2).Select(i => i.Path).ToHashSet();
        // The phone still "answers" but every file after the first two fails.
        phone.BeforeRead = (path, _) =>
        {
            if (!good.Contains(path)) throw new IOException("Simulated phone glitch");
        };

        var jobs = items.Select(i => new TransferJob(i)).ToList();
        var progress = new TransferProgress();
        var summary = await new TransferManager(null).RunAsync(phone, jobs, new TransferOptions(dest.Path, true, false),
            progress, new PauseGate(), CancellationToken.None);

        Assert.Equal(2, summary.Copied);
        Assert.Equal(0, summary.Failed);                // not 18 bogus failures
        Assert.Equal(18, summary.Remaining);            // all kept for Resume
        Assert.NotNull(summary.StopReason);
        Assert.Equal(0, progress.Snapshot().Failed);
        Assert.Equal(2, progress.Snapshot().FilesDone);

        // After a reconnect, Resume finishes the rest.
        phone.BeforeRead = null;
        var resumed = await Run(phone, jobs, dest.Path);
        Assert.Equal(18, resumed.Copied);
        Assert.Equal(20, Directory.GetFiles(dest.Path).Length);
    }

    [Fact]
    public async Task Progress_reaches_100_percent_including_skipped_files()
    {
        using var dest = new TempFolder();
        var phone = new FakePhone();
        var a = phone.AddFile("DCIM/A.jpg", TestData.Bytes(4000, 14));
        var b = phone.AddFile("DCIM/B.jpg", TestData.Bytes(6000, 15));
        File.WriteAllBytes(Path.Combine(dest.Path, "A.jpg"), TestData.Bytes(4000, 14));
        var progress = new TransferProgress();

        await new TransferManager(null).RunAsync(phone, new[] { new TransferJob(a), new TransferJob(b) },
            new TransferOptions(dest.Path, true, false), progress, new PauseGate(), CancellationToken.None);

        var snapshot = progress.Snapshot();
        Assert.Equal(snapshot.BytesTotal, snapshot.BytesProcessed);
        Assert.Equal(2, snapshot.FilesDone);
        Assert.Equal(6000, snapshot.BytesCopied);
        Assert.Equal(1, snapshot.Skipped);
    }
}
