using System.Diagnostics;
using System.IO;
using AndroidPhotoTransfer.Core.Wpd;
using AndroidPhotoTransfer.Data;
using AndroidPhotoTransfer.Infrastructure;

namespace AndroidPhotoTransfer.Core.Transfer
{
    /// <summary>
    /// The transfer engine. One file at a time (MTP is the bottleneck; reliability first):
    /// stream in 1 MB chunks to "name.partial" → verify size → rename to the final name → record history.
    /// Interrupted files are always deleted, so a ".partial" never looks like a finished photo.
    /// </summary>
    internal sealed class TransferManager : ITransferManager
    {
        private const int BufferSize = 1024 * 1024;
        private const long FreeSpaceMargin = 50L * 1024 * 1024;

        /// <summary>This many failures in a row means the phone has a problem, not the files: stop and keep the rest for Resume.</summary>
        internal const int ConsecutiveFailureLimit = 5;

        private static readonly TimeSpan ReadTimeout = TimeSpan.FromSeconds(60);

        private readonly IHistoryDatabase? _history;

        public TransferManager(IHistoryDatabase? history)
        {
            _history = history;
        }

        public async Task<TransferSummary> RunAsync(IWpdAdapter device, IReadOnlyList<TransferJob> jobs, TransferOptions options,
            TransferProgress progress, PauseGate pause, CancellationToken ct)
        {
            var stopwatch = Stopwatch.StartNew();
            var pending = jobs.Where(j => j.State == TransferState.Pending).ToList();

            Directory.CreateDirectory(options.DestinationFolder);
            EnsureFreeSpace(pending, options);

            progress.Begin(pending.Count, pending.Sum(j => j.Item.Size));
            Logger.Info($"Transfer started: {pending.Count:N0} file(s), {FileUtilities.FormatBytes(pending.Sum(j => j.Item.Size))}");

            var buffers = new[] { new byte[BufferSize], new byte[BufferSize] };
            bool disconnected = false;
            string? stopReason = null;
            long bytesCopied = 0;
            var failedInARow = new List<TransferJob>();

            foreach (var job in pending)
            {
                if (ct.IsCancellationRequested || disconnected || stopReason != null) break;

                job.State = TransferState.Transferring;
                job.Error = null;
                progress.BeginFile(job.Item);

                try
                {
                    await TransferWithPauseAsync(device, job, options, buffers, progress, pause, ct);
                    if (job.State == TransferState.Complete) bytesCopied += job.Item.Size;
                    failedInARow.Clear();
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    job.State = TransferState.Cancelled;
                }
                catch (Exception ex) when (ErrorMessages.IsDestinationFatal(ex))
                {
                    Logger.Error($"Transfer stopped: destination problem while copying {job.Item.Name}", ex);
                    Fail(job, ex);
                    stopReason = ErrorMessages.Describe(ex);
                }
                catch (Exception ex)
                {
                    if (ex is DeviceDisconnectedException || !await device.IsStillConnectedAsync())
                    {
                        Logger.Warn($"Phone disconnected while copying {job.Item.Name}", ex);
                        progress.RollbackFile();
                        job.State = TransferState.Pending;
                        disconnected = true;
                    }
                    else
                    {
                        Logger.Error($"Transfer failed: {job.Item.Name}", ex);
                        Fail(job, ex);
                        failedInARow.Add(job);
                        if (failedInARow.Count >= ConsecutiveFailureLimit)
                        {
                            // The phone is answering but every file fails: that's a phone/connection problem.
                            // Put these back in the queue so Resume retries them after a reconnect.
                            Logger.Warn($"Stopping: {failedInARow.Count} files failed in a row");
                            foreach (var failed in failedInARow)
                            {
                                failed.State = TransferState.Pending;
                                failed.Error = null;
                            }
                            // Earlier ones were already counted as failed; the current one hasn't been counted yet.
                            progress.UndoFailedFiles(failedInARow.Take(failedInARow.Count - 1).Select(j => j.Item.Size));
                            progress.RollbackFile();
                            stopReason = "The phone stopped responding. Unplug it, plug it back in and unlock it, then click Resume.";
                            break;
                        }
                    }
                }

                progress.EndFile(job.State);
            }

            if (ct.IsCancellationRequested)
            {
                foreach (var job in pending.Where(j => j.State == TransferState.Pending)) job.State = TransferState.Cancelled;
            }

            var summary = new TransferSummary(
                Copied: pending.Count(j => j.State == TransferState.Complete),
                Skipped: pending.Count(j => j.State == TransferState.Skipped),
                Failed: pending.Count(j => j.State == TransferState.Failed),
                Cancelled: pending.Count(j => j.State == TransferState.Cancelled),
                Remaining: pending.Count(j => j.State == TransferState.Pending),
                BytesCopied: bytesCopied,
                DeviceDisconnected: disconnected,
                StopReason: stopReason,
                Elapsed: stopwatch.Elapsed);

            Logger.Info($"Transfer finished in {stopwatch.Elapsed:hh\\:mm\\:ss}: {summary.Copied} copied, {summary.Skipped} skipped, " +
                        $"{summary.Failed} failed, {summary.Cancelled} cancelled, {summary.Remaining} remaining" +
                        (disconnected ? " (phone disconnected)" : "") + (stopReason != null ? $" (stopped: {stopReason})" : "") +
                        $", average {FileUtilities.FormatBytes((long)(bytesCopied / Math.Max(1, stopwatch.Elapsed.TotalSeconds)))}/s");
            return summary;
        }

        private void EnsureFreeSpace(IReadOnlyList<TransferJob> jobs, TransferOptions options)
        {
            long needed = jobs
                .Where(j => !options.SkipExisting || !DestinationResolver.AlreadyExists(DestinationFolderFor(j, options), j.Item.Name, j.Item.Size))
                .Sum(j => j.Item.Size);
            var available = FileUtilities.GetAvailableFreeSpace(options.DestinationFolder);
            if (available.HasValue && needed + FreeSpaceMargin > available.Value)
            {
                throw new InsufficientSpaceException(needed, available.Value);
            }
        }

        /// <summary>
        /// Waits while paused. If the user pauses mid-file, the half-copied file is discarded and the same file
        /// starts again from the beginning on resume (the phone never sits with an open transfer).
        /// </summary>
        private async Task TransferWithPauseAsync(IWpdAdapter device, TransferJob job, TransferOptions options, byte[][] buffers,
            TransferProgress progress, PauseGate pause, CancellationToken ct)
        {
            while (true)
            {
                if (pause.IsPaused) Logger.Info("Transfer paused");
                bool wasPaused = pause.IsPaused;
                await pause.WaitAsync(ct);
                if (wasPaused) Logger.Info("Transfer resumed");

                try
                {
                    await TransferWithRetryAsync(device, job, options, buffers, progress, pause.Token, ct);
                    return;
                }
                catch (OperationCanceledException) when (!ct.IsCancellationRequested && pause.IsPaused)
                {
                    progress.RollbackFile();
                    job.State = TransferState.Transferring;
                    Logger.Info($"Paused while copying {job.Item.Name}; it will restart when resumed");
                }
            }
        }

        private async Task TransferWithRetryAsync(IWpdAdapter device, TransferJob job, TransferOptions options, byte[][] buffers,
            TransferProgress progress, CancellationToken pauseToken, CancellationToken ct)
        {
            try
            {
                await TransferOneAsync(device, job, options, buffers, progress, pauseToken, ct);
            }
            catch (Exception ex) when (ex is not OperationCanceledException && ex is not DeviceDisconnectedException &&
                                       !ErrorMessages.IsDestinationFatal(ex))
            {
                if (!await device.IsStillConnectedAsync()) throw new DeviceDisconnectedException(inner: ex);
                Logger.Warn($"Retrying {job.Item.Name} after a failed attempt", ex);
                progress.RollbackFile();
                await Task.Delay(500, ct);
                await TransferOneAsync(device, job, options, buffers, progress, pauseToken, ct);
            }
        }

        private async Task TransferOneAsync(IWpdAdapter device, TransferJob job, TransferOptions options, byte[][] buffers,
            TransferProgress progress, CancellationToken pauseToken, CancellationToken ct)
        {
            var item = job.Item;
            var folder = DestinationFolderFor(job, options);
            Directory.CreateDirectory(folder);

            var (finalPath, isDuplicate) = DestinationResolver.Resolve(folder, item.Name, item.Size, options.SkipExisting);
            if (isDuplicate)
            {
                job.State = TransferState.Skipped;
                job.DestinationPath = finalPath;
                RecordHistory(device, job, finalPath);
                return;
            }

            using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct, pauseToken);
            var token = linked.Token;
            var partialPath = finalPath + ".partial";
            Task<int>? pendingRead = null;
            try
            {
                long written;
                await using (var source = await device.OpenReadAsync(item.Path, item.PersistentId, token).WaitAsync(ReadTimeout, token))
                await using (var target = new FileStream(partialPath, FileMode.Create, FileAccess.Write, FileShare.None,
                                 bufferSize: 1, FileOptions.Asynchronous | FileOptions.SequentialScan))
                {
                    // Read the next chunk from the phone while the previous one is being written to the PC.
                    var front = buffers[0];
                    var back = buffers[1];
                    pendingRead = source.ReadAsync(front, 0, front.Length, token).WaitAsync(ReadTimeout, token);
                    while (true)
                    {
                        int read = await pendingRead;
                        pendingRead = null;
                        if (read <= 0) break;

                        var filled = front;
                        (front, back) = (back, front);
                        pendingRead = source.ReadAsync(front, 0, front.Length, token).WaitAsync(ReadTimeout, token);

                        await target.WriteAsync(filled.AsMemory(0, read), token);
                        progress.AddBytes(read);
                    }
                    await target.FlushAsync(token);
                    written = target.Length;
                }

                if (item.Size > 0 && written != item.Size)
                {
                    throw new TransferVerificationException($"Size mismatch for {item.Name}: expected {item.Size} bytes, wrote {written}.");
                }

                File.Move(partialPath, finalPath, overwrite: false);
            }
            catch
            {
                // Don't leave an unobserved read behind, and never leave a half-written file.
                pendingRead?.ContinueWith(t => _ = t.Exception, TaskScheduler.Default);
                FileUtilities.TryDelete(partialPath);
                throw;
            }

            ApplyTimestamps(finalPath, item.Created, item.Date);
            job.State = TransferState.Complete;
            job.DestinationPath = finalPath;
            RecordHistory(device, job, finalPath);
        }

        private static string DestinationFolderFor(TransferJob job, TransferOptions options) =>
            options.OrganizeByFolder && job.Item.Folder.Length > 0
                ? Path.Combine(options.DestinationFolder, FileUtilities.SanitizeRelativeFolder(job.Item.Folder))
                : options.DestinationFolder;

        private void RecordHistory(IWpdAdapter device, TransferJob job, string destination)
        {
            try
            {
                _history?.Record(device.Info.Key, job.Item, destination);
            }
            catch (Exception ex)
            {
                Logger.Warn("Could not record transfer history", ex);
            }
        }

        /// <summary>Keep the file's original date so it sorts correctly in Explorer and photo apps.</summary>
        private static void ApplyTimestamps(string path, DateTime? created, DateTime? modified)
        {
            try
            {
                if (modified.HasValue) File.SetLastWriteTime(path, modified.Value);
                if ((created ?? modified).HasValue) File.SetCreationTime(path, (created ?? modified)!.Value);
            }
            catch (Exception ex)
            {
                Logger.Warn("Could not set file dates", ex);
            }
        }

        private static void Fail(TransferJob job, Exception ex)
        {
            job.State = TransferState.Failed;
            job.Error = ErrorMessages.Describe(ex);
        }
    }
}
