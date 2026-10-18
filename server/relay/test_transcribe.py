"""transcribe.Transcriber against a fake Transcribe client: partial then final results reach the
callback with epoch times, a stream closes after IDLE without audio, the next chunk opens a new one,
and a refused stream backs off instead of retrying every chunk.
    python test_transcribe.py
"""
import asyncio
import time
from types import SimpleNamespace as NS

import transcribe
from transcribe import Transcriber

transcribe.IDLE = 0.2


class FakeStream:
    def __init__(self):
        self.audio, self.ended, self.out = [], False, asyncio.Queue()
        self.input_stream = NS(send_audio_event=self.send, end_stream=self.end)
        self.output_stream = self.events()

    async def send(self, audio_chunk):
        self.audio.append(audio_chunk)
        if len(self.audio) == 1:            # Transcribe answers: a partial, then the final
            alt = lambda t: NS(alternatives=[NS(transcript=t)], result_id="r1", start_time=0.5, end_time=1.25)
            await self.out.put([NS(**vars(alt("hello")), is_partial=True)])
            await self.out.put([NS(**vars(alt("hello team")), is_partial=False)])

    async def end(self):
        self.ended = True
        await self.out.put(None)

    async def events(self):
        while (results := await self.out.get()) is not None:
            yield NS(transcript=NS(results=results))


class FakeClient:
    def __init__(self, fail=False):
        self.streams, self.fail = [], fail

    async def start_stream_transcription(self, **kw):
        assert kw == {"language_code": "en-US", "media_sample_rate_hz": 16000, "media_encoding": "pcm"}, kw
        if self.fail:
            raise PermissionError("not allowed")
        self.streams.append(FakeStream())
        return self.streams[-1]


async def main():
    got = []
    async def on_result(*a):
        got.append(a)
    c = FakeClient()
    t = Transcriber(on_result, client=c)
    t0 = time.time()
    t.feed("A", b"\0\0" * 640)
    t.feed("A", b"\0\0" * 640)
    await asyncio.sleep(0.5)                # past IDLE: stream closed
    assert len(c.streams) == 1 and c.streams[0].ended and len(c.streams[0].audio) == 2
    assert [(w, rid, txt, part) for w, rid, txt, part, *_ in got] == [("A", "r1", "hello", True), ("A", "r1", "hello team", False)], got
    start, end = got[1][4:]
    assert abs(start - (t0 + 0.5)) < 0.1 and abs(end - (t0 + 1.25)) < 0.1, (start - t0, end - t0)
    t.feed("A", b"\0\0" * 640)              # talking again: a fresh stream
    await asyncio.sleep(0.4)
    assert len(c.streams) == 2 and not t.queues

    bad = Transcriber(on_result, client=FakeClient(fail=True))
    bad.feed("B", b"\0\0")
    await asyncio.sleep(0.05)
    bad.feed("B", b"\0\0")                  # backing off: dropped, no new attempt
    assert not bad.queues and bad.down_until["B"] > time.time()
    print("transcribe ok: partial + final with epoch times, idle close, reopen, back-off")


asyncio.run(main())
