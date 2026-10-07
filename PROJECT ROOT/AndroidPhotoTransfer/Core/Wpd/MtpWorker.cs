using System.Diagnostics;
using System.Runtime.CompilerServices;
using AndroidPhotoTransfer.Infrastructure;

namespace AndroidPhotoTransfer.Core.Wpd
{
    /// <summary>
    /// Runs every WPD/MTP call on one dedicated background thread.
    /// WPD COM objects must not be used from several threads at once, and a slow phone must never
    /// block the UI thread. Interactive work (thumbnails, previews) can jump ahead of bulk work
    /// (scanning, transfer chunks) by using <c>highPriority</c>.
    /// </summary>
    public sealed class MtpWorker : IDisposable
    {
        private static readonly TimeSpan SlowCallThreshold = TimeSpan.FromSeconds(3);
        private readonly Queue<Action> _high = new();
        private readonly Queue<Action> _normal = new();
        private readonly object _gate = new();
        private readonly Thread _thread;
        private bool _disposed;

        public MtpWorker()
        {
            _thread = new Thread(Loop) { IsBackground = true, Name = "MTP worker" };
            _thread.SetApartmentState(ApartmentState.MTA);
            _thread.Start();
        }

        public Task<T> RunAsync<T>(Func<T> work, CancellationToken ct = default, bool highPriority = false,
            [CallerMemberName] string caller = "")
        {
            var tcs = new TaskCompletionSource<T>(TaskCreationOptions.RunContinuationsAsynchronously);

            void Execute()
            {
                if (ct.IsCancellationRequested)
                {
                    tcs.TrySetCanceled(ct);
                    return;
                }
                long started = Stopwatch.GetTimestamp();
                try
                {
                    tcs.TrySetResult(work());
                }
                catch (Exception ex)
                {
                    tcs.TrySetException(ex);
                }
                var elapsed = Stopwatch.GetElapsedTime(started);
                if (elapsed > SlowCallThreshold) Logger.Warn($"Slow device call: {caller} took {elapsed.TotalSeconds:F1}s");
            }

            lock (_gate)
            {
                ObjectDisposedException.ThrowIf(_disposed, this);
                (highPriority ? _high : _normal).Enqueue(Execute);
                Monitor.Pulse(_gate);
            }
            return tcs.Task;
        }

        public Task RunAsync(Action work, CancellationToken ct = default, bool highPriority = false,
            [CallerMemberName] string caller = "") =>
            RunAsync(() =>
            {
                work();
                return true;
            }, ct, highPriority, caller);

        /// <summary>Blocking variant for synchronous Stream APIs. Safe to call from the worker itself.</summary>
        public T Run<T>(Func<T> work) =>
            Thread.CurrentThread == _thread ? work() : RunAsync(work).GetAwaiter().GetResult();

        private void Loop()
        {
            while (true)
            {
                Action next;
                lock (_gate)
                {
                    while (_high.Count == 0 && _normal.Count == 0 && !_disposed) Monitor.Wait(_gate);
                    if (_high.Count == 0 && _normal.Count == 0) return;
                    next = _high.Count > 0 ? _high.Dequeue() : _normal.Dequeue();
                }
                next();
            }
        }

        public void Dispose()
        {
            lock (_gate)
            {
                _disposed = true;
                Monitor.PulseAll(_gate);
            }
        }
    }
}
