using System.IO;
using System.Runtime.InteropServices;

namespace AndroidPhotoTransfer.Infrastructure
{
    /// <summary>The phone went away (unplugged, USB mode changed, or it stopped answering).</summary>
    public sealed class DeviceDisconnectedException : Exception
    {
        public DeviceDisconnectedException(string message = "The phone is no longer connected.", Exception? inner = null)
            : base(message, inner) { }
    }

    public sealed class InsufficientSpaceException : Exception
    {
        public InsufficientSpaceException(long required, long available)
            : base($"Not enough free space: {required} bytes needed, {available} available.")
        {
            Required = required;
            Available = available;
        }

        public long Required { get; }
        public long Available { get; }
    }

    public sealed class TransferVerificationException : Exception
    {
        public TransferVerificationException(string message) : base(message) { }
    }

    /// <summary>Translates exceptions into messages a normal person can act on. Technical detail goes to the log.</summary>
    internal static class ErrorMessages
    {
        public const string PhoneStoppedResponding =
            "The phone stopped responding. Unlock your phone and make sure USB is still set to File Transfer.";

        public static string Describe(Exception ex) => ex switch
        {
            InsufficientSpaceException s =>
                $"Not enough free space on the PC. Needed: {FileUtilities.FormatBytes(s.Required)}, available: {FileUtilities.FormatBytes(s.Available)}.",
            DeviceDisconnectedException => "The phone was disconnected. Reconnect it and make sure USB is set to File Transfer.",
            TransferVerificationException => "The copied file was incomplete, so it was discarded. Try again.",
            UnauthorizedAccessException => "Windows didn't allow saving to that folder. Choose a different destination folder.",
            DirectoryNotFoundException or DriveNotFoundException =>
                "The destination folder is no longer available. If it's on an external drive, reconnect it or choose another folder.",
            IOException when IsDiskFull(ex) => "The PC drive is full. Free up space or choose another destination folder.",
            TimeoutException => PhoneStoppedResponding,
            COMException => PhoneStoppedResponding,
            _ when IsDeviceError(ex) => PhoneStoppedResponding,
            _ => $"Something went wrong: {ex.Message}"
        };

        public static bool IsDiskFull(Exception ex)
        {
            int code = ex.HResult & 0xFFFF;
            return code is 112 /* ERROR_DISK_FULL */ or 39 /* ERROR_HANDLE_DISK_FULL */;
        }

        /// <summary>Destination-side problems that will fail every remaining file, so the transfer should stop.</summary>
        public static bool IsDestinationFatal(Exception ex) =>
            IsDiskFull(ex) || ex is UnauthorizedAccessException or DirectoryNotFoundException or DriveNotFoundException;

        private static bool IsDeviceError(Exception ex) =>
            (ex.HResult & unchecked((int)0xFFFF0000)) == unchecked((int)0x802A0000); // FACILITY_WPD
    }
}
