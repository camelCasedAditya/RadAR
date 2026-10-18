"""Live transcription of the voice call with AWS Transcribe streaming. Each talker (phone id, or "hub")
gets a stream while they push-to-talk: call audio (kind 7) is already 16 kHz mono int16 PCM, which is
what Transcribe takes. The stream opens on the first chunk and ends after IDLE seconds without audio,
so only speech is billed. Results go to on_result(who, result_id, text, partial, start, end) with
start/end as epoch seconds, on the same clock as the session file's records.

Enabled on the relay with RADAR_TRANSCRIBE=1 (the EC2 live box; its instance role may call
transcribe:StartStreamTranscription). Needs `pip install amazon-transcribe`.
"""
import asyncio
import time

IDLE = 1.5          # s without audio from a talker: their utterance is over, close the stream
RETRY = 30          # s to wait after Transcribe refused a stream (no permission, throttled)


class Transcriber:
    def __init__(self, on_result, region="us-east-2", client=None, language="en-US"):
        self.on_result = on_result
        self.language = language
        self._client = client
        self.region = region
        self.queues: dict[str, asyncio.Queue] = {}
        self.down_until: dict[str, float] = {}

    def client(self):
        if self._client is None:
            from amazon_transcribe.client import TranscribeStreamingClient
            self._client = TranscribeStreamingClient(region=self.region)
        return self._client

    def feed(self, who, pcm):
        """One audio chunk from `who`. Call from the event loop."""
        if time.time() < self.down_until.get(who, 0):
            return
        q = self.queues.get(who)
        if q is None:
            q = self.queues[who] = asyncio.Queue()
            asyncio.create_task(self._run(who, q, time.time()))
        q.put_nowait(pcm)

    async def _run(self, who, q, t0):
        try:
            stream = await self.client().start_stream_transcription(
                language_code=self.language, media_sample_rate_hz=16000, media_encoding="pcm")

            async def pump():
                while True:
                    try:
                        pcm = await asyncio.wait_for(q.get(), IDLE)
                    except asyncio.TimeoutError:
                        break
                    await stream.input_stream.send_audio_event(audio_chunk=pcm)
                if self.queues.get(who) is q:   # new audio from now on opens a new stream
                    del self.queues[who]
                await stream.input_stream.end_stream()

            async def read():
                async for event in stream.output_stream:
                    for r in event.transcript.results:
                        text = r.alternatives[0].transcript if r.alternatives else ""
                        if text:
                            await self.on_result(who, r.result_id, text, r.is_partial, t0 + r.start_time, t0 + r.end_time)

            await asyncio.gather(pump(), read())
        except Exception as e:
            print(f"transcribe {who}: {type(e).__name__}: {e}", flush=True)
            self.down_until[who] = time.time() + RETRY
        finally:
            if self.queues.get(who) is q:
                del self.queues[who]
