using AndroidPhotoTransfer.Core.Wpd;

namespace AndroidPhotoTransfer.Core.Devices
{
    public enum ConnectionState
    {
        /// <summary>No phone attached (or it is in charge-only mode).</summary>
        Waiting,
        Connecting,
        /// <summary>Windows sees the phone but its storage is unavailable (locked, or USB mode not File Transfer).</summary>
        Locked,
        Ready,
        Error
    }

    public interface IDeviceManager : IDisposable
    {
        ConnectionState State { get; }
        IWpdAdapter? Current { get; }
        IReadOnlyList<PhoneListing> Phones { get; }
        IReadOnlyList<StorageRoot> StorageRoots { get; }
        string? ErrorMessage { get; }

        /// <summary>Raised on a background thread whenever any property above changes.</summary>
        event EventHandler? Changed;

        /// <summary>Re-reads the device list; call when Windows reports a device change.</summary>
        Task RefreshAsync();

        /// <summary>Drops the current connection and connects again (fresh session, then a fresh scan).</summary>
        Task ReconnectAsync();

        Task SelectPhoneAsync(string deviceId);
    }
}
