using AndroidPhotoTransfer.Core.Media;
using AndroidPhotoTransfer.Core.Wpd;

namespace AndroidPhotoTransfer.Core.Transfer
{
    public enum TransferState { Pending, Transferring, Complete, Skipped, Failed, Cancelled }

    public sealed class TransferJob
    {
        public TransferJob(MediaItem item) => Item = item;

        public MediaItem Item { get; }
        public TransferState State { get; set; } = TransferState.Pending;
        public string? DestinationPath { get; set; }
        public string? Error { get; set; }
    }

    public sealed record TransferOptions(string DestinationFolder, bool SkipExisting, bool OrganizeByFolder);

    public sealed record TransferSummary(
        int Copied,
        int Skipped,
        int Failed,
        int Cancelled,
        int Remaining,
        long BytesCopied,
        bool DeviceDisconnected,
        string? StopReason,
        TimeSpan Elapsed);

    public interface ITransferManager
    {
        /// <summary>
        /// Copies every <see cref="TransferState.Pending"/> job, one file at a time.
        /// Throws <see cref="Infrastructure.InsufficientSpaceException"/> before copying anything if the PC lacks space.
        /// If the phone disconnects, unfinished jobs stay Pending so the same jobs can be resumed later.
        /// </summary>
        Task<TransferSummary> RunAsync(IWpdAdapter device, IReadOnlyList<TransferJob> jobs, TransferOptions options,
            TransferProgress progress, PauseGate pause, CancellationToken ct);
    }
}
