using System.IO;
using AndroidPhotoTransfer.Core.Devices;
using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Transfer;
using AndroidPhotoTransfer.Core.Wpd;
using Xunit.Abstractions;

namespace AndroidPhotoTransfer.Tests;

/// <summary>
/// End-to-end check against a real Windows Portable Device (read-only on the device).
/// Opt-in: set APT_TEST_REAL_DEVICE=1. Set APT_INCLUDE_STORAGE_DEVICES=1 to use a USB drive when no phone is attached.
/// </summary>
public class RealDeviceTests
{
    private readonly ITestOutputHelper _output;

    public RealDeviceTests(ITestOutputHelper output) => _output = output;

    [Fact]
    public async Task Connects_scans_and_copies_from_a_real_device()
    {
        if (Environment.GetEnvironmentVariable("APT_TEST_REAL_DEVICE") != "1")
        {
            _output.WriteLine("Skipped: set APT_TEST_REAL_DEVICE=1 to run against a connected device.");
            return;
        }

        bool includeStorage = Environment.GetEnvironmentVariable("APT_INCLUDE_STORAGE_DEVICES") == "1";
        using var worker = new MtpWorker();
        using var devices = new DeviceManager(worker, includeStorage);
        await devices.RefreshAsync();
        _output.WriteLine($"State: {devices.State}, phones: {string.Join(", ", devices.Phones.Select(p => p.Name))}");
        Assert.Equal(ConnectionState.Ready, devices.State);

        var device = devices.Current!;
        _output.WriteLine($"Connected: {device.Info.Name} key={device.Info.Key}");
        foreach (var root in devices.StorageRoots) _output.WriteLine($"Storage: {root.Name} [{root.Path}]");

        // Scan, but stop once we have a few media files so this stays quick on big devices.
        var found = new List<MediaItem>();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(60));
        try
        {
            await new MediaScanner().ScanAsync(device, devices.StorageRoots, batch =>
            {
                lock (found) found.AddRange(batch);
                if (found.Count(f => f.Size is > 0 and < 8_000_000) >= 3) cts.Cancel();
            }, cts.Token);
        }
        catch (OperationCanceledException)
        {
        }

        _output.WriteLine($"Found {found.Count} media item(s)");
        foreach (var item in found.Take(10)) _output.WriteLine($"  {item.Folder}/{item.Name} {item.Size} bytes {item.Date} [{item.Category}]");
        Assert.NotEmpty(found);

        var sample = found.Where(f => f.Size is > 0 and < 8_000_000).Take(3).ToList();
        var thumb = await device.GetThumbnailAsync(sample[0].Path, sample[0].PersistentId, default);
        _output.WriteLine($"Device thumbnail for {sample[0].Name}: {(thumb == null ? "none" : thumb.Length + " bytes")}");

        using var dest = new TempFolder();
        var jobs = sample.Select(s => new TransferJob(s)).ToList();
        var summary = await new TransferManager(null).RunAsync(device, jobs, new TransferOptions(dest.Path, true, false),
            new TransferProgress(), new PauseGate(), default);
        _output.WriteLine($"Copied {summary.Copied}, failed {summary.Failed}: {string.Join("; ", jobs.Select(j => j.Error))}");

        Assert.Equal(sample.Count, summary.Copied);
        foreach (var job in jobs) Assert.Equal(job.Item.Size, new FileInfo(job.DestinationPath!).Length);
        Assert.Empty(Directory.GetFiles(dest.Path, "*.partial"));
    }
}
