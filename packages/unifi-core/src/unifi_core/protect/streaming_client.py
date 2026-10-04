"""Close SDK video responses even when a bounded sink aborts or is cancelled."""

from uiprotect import ProtectApiClient


class StreamingProtectApiClient(ProtectApiClient):
    """uiprotect 16.10 closes streamed responses only on the success path.

    Keep its request, authentication, channel and camera permission handling;
    extend only the stream lifecycle. This is a subclass, not a monkey patch.
    Recheck this narrow SDK hook when upgrading uiprotect.
    """

    async def _stream_response(self, response, chunk_size, iterator_callback=None, progress_callback=None):
        try:
            return await super()._stream_response(response, chunk_size, iterator_callback, progress_callback)
        finally:
            response.close()
