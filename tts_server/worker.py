"""GPU process. Exiting this process also releases vLLM's child processes."""

from __future__ import annotations

import os
import traceback


def validate_audio_tokens(ids):
    # The second codebook has 4096 entries, not 1024. Upstream's debug logging
    # counts a smaller range, but CosyVoice's embedding supports all 5120 codes.
    if not ids or any(t < 65536 or t >= 65536 + 1024 + 4096 for t in ids):
        raise ValueError("Model returned invalid audio tokens; try another reference or seed")


def run_worker(connection):
    # The HTTP server deliberately never imports CUDA libraries.
    if os.name == "posix":
        os.setsid()
    try:
        import io
        import random
        import sys
        import wave
        from functools import lru_cache
        import numpy as np
        import torch
        from huggingface_hub import snapshot_download

        sys.path.insert(0, os.environ.get("STEP_AUDIO_SOURCE", "/opt/Step-Audio-EditX"))
        from tokenizer import StepAudioTokenizer
        from tts import StepAudioTTS
        from vllm import SamplingParams

        context_size = int(os.environ.get("STEP_AUDIO_CONTEXT_SIZE", "4096"))

        class Narrator(StepAudioTTS):
            # clone() does not expose sampling arguments. Override only this
            # upstream hook, retaining its prompt, tokenizer and vocoder paths.
            temperature = 0.7
            seed = 12345

            @lru_cache(maxsize=4)
            def preprocess_prompt_wav(self, prompt_wav_path):
                # Reference IDs are immutable. Reuse their encoded tokens and
                # speaker features across a book instead of re-encoding on
                # every chunk (which can subtly vary token boundaries).
                torch.manual_seed(12345)
                torch.cuda.manual_seed_all(12345)
                return super().preprocess_prompt_wav(prompt_wav_path)

            def _generate(self, token_ids, max_tokens=4096, temperature=0.7):
                budget = min(max_tokens, 1500, context_size - len(token_ids))
                if budget < 100:
                    raise ValueError("Reference/text exceeds model context; use a shorter reference")
                sampling = SamplingParams(
                    temperature=self.temperature, seed=self.seed,
                    max_tokens=budget, skip_special_tokens=False,
                )
                result = self.llm.generate(
                    [{"prompt_token_ids": token_ids}], sampling, use_tqdm=False,
                )[0].outputs[0]
                if result.finish_reason == "length":
                    raise ValueError("Generation hit its token limit; shorten the chunk or reference")
                ids = list(result.token_ids)
                if ids and ids[-1] == 3:  # upstream <|EOT|>
                    ids.pop()
                validate_audio_tokens(ids)
                # Reference cache hits must not change the decoder noise draw.
                torch.manual_seed(self.seed)
                torch.cuda.manual_seed_all(self.seed)
                return torch.tensor(ids, dtype=torch.long)

        def model_path(variable, repository):
            configured = os.environ.get(variable)
            if configured:
                return configured
            return snapshot_download(repository)

        tokenizer_path = model_path("STEP_AUDIO_TOKENIZER_PATH", "stepfun-ai/Step-Audio-Tokenizer")
        editx_path = model_path("STEP_AUDIO_MODEL_PATH", "stepfun-ai/Step-Audio-EditX")
        tokenizer = StepAudioTokenizer(tokenizer_path, model_source="local")
        model = Narrator(
            editx_path, tokenizer, model_source="local",
            dtype="bfloat16", quantization=None,
            gpu_memory_utilization=float(os.environ.get("STEP_AUDIO_GPU_MEMORY_UTILIZATION", "0.4")),
            max_model_len=context_size, max_num_seqs=1,
            enforce_eager=True, cosyvoice_dtype="bfloat16", cosyvoice_cuda_graph=False,
        )
        connection.send({"ready": True})
        while True:
            request = connection.recv()
            try:
                model.temperature = request["temperature"]
                model.seed = request["seed"]
                random.seed(model.seed)
                np.random.seed(model.seed)
                torch.manual_seed(model.seed)
                torch.cuda.manual_seed_all(model.seed)
                with torch.inference_mode():
                    audio, rate = model.clone(
                        prompt_wav_path=request["reference_path"],
                        prompt_text=request["transcript"], target_text=request["text"],
                    )
                samples = audio.detach().float().cpu().numpy().reshape(-1)
                if rate != 24000 or not 0.1 <= samples.size / rate <= 30:
                    raise ValueError("Invalid output length or sample rate; shorten the narration chunk")
                if not np.isfinite(samples).all() or np.max(np.abs(samples)) < 1e-5:
                    raise ValueError("Model returned silent or non-finite audio")
                # Only remove long outer silence; retain quiet consonants and
                # breaths with 100 ms padding. Never gate internal speech.
                active = np.flatnonzero(np.abs(samples) > 0.001)
                if active.size:
                    pad = int(rate * 0.1)
                    samples = samples[max(0, active[0] - pad):min(samples.size, active[-1] + pad + 1)]
                fade = min(int(rate * 0.005), samples.size // 2)
                samples[:fade] *= np.linspace(0, 1, fade)
                samples[-fade:] *= np.linspace(1, 0, fade)
                peak = np.max(np.abs(samples))
                if peak > 0.98:
                    samples *= 0.98 / peak
                output = io.BytesIO()
                with wave.open(output, "wb") as wav:
                    wav.setparams((1, 2, rate, 0, "NONE", "not compressed"))
                    wav.writeframes((samples * 32767).round().astype("<i2").tobytes())
                connection.send({"audio": output.getvalue()})
            except Exception as exc:
                traceback.print_exc()
                connection.send({"error": str(exc)})
    except EOFError:
        pass
    except BaseException as exc:
        traceback.print_exc()
        try:
            connection.send({"error": str(exc)})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()
