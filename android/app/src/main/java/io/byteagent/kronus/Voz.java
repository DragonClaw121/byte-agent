package io.byteagent.kronus;

import android.content.Context;
import android.content.SharedPreferences;
import android.media.AudioAttributes;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.media.ToneGenerator;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Base64;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Motor de voz do Kronus dentro de uma CHAMADA (fora da tela): usado pelas chamadas recebidas ("Kronus te liga"),
 * que podem acontecer com o app fechado. Ouve pelo relógio, fala com o servidor e toca a voz do Kronus (C1) ou a voz
 * do Android (C2). Mesma lógica testada no app 1.10.
 */
final class Voz {
    static final int RATE = 16000;

    interface Ui {
        void state(String text);
        void log(String who, String text);
    }

    private final Context ctx;
    private final SharedPreferences prefs;
    private final Ui ui;
    private TextToSpeech tts;
    private volatile boolean ttsReady = false;
    volatile boolean listening = false;

    Voz(Context ctx, Ui ui) {
        this.ctx = ctx.getApplicationContext();
        this.prefs = this.ctx.getSharedPreferences("kronus", Context.MODE_PRIVATE);
        this.ui = ui;
        tts = new TextToSpeech(this.ctx, status -> ttsReady = status == TextToSpeech.SUCCESS);
    }

    void close() {
        try { tts.shutdown(); } catch (Exception ignored) { }
    }

    private boolean waitTts() {
        for (int i = 0; i < 40 && !ttsReady; i++) sleep(100);
        return ttsReady;
    }

    static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) { }
    }

    String method() {
        String m = prefs.getString("metodo", "C1");
        return "C2".equals(m) ? "C2" : "C1";
    }

    // ------------------------------------------------------------------ chamada recebida
    /** Atendeu: o Kronus lê o aviso e ouve uma resposta (ex.: "me lembra de novo em 10 minutos"). */
    void announceAndListen(KronusCallService.KronusConnection k, String announce) {
        try {
            k.waitBluetooth(4000);
            sleep(1200);                                   // o canal do relógio estabiliza
            ui.state("Kronus avisando...");
            ui.log("Kronus", announce);
            playInCall(serverVoice(announce), announce);
            if (k.ended) return;
            ui.state("Pode responder depois do bipe...");
            beeps(1);
            listening = true;
            byte[] pcm = record(6000, true);
            listening = false;
            if (pcm == null || k.ended) return;
            ui.state("Pensando...");
            JSONObject res = http(server() + "/api/v1/voice?audio=wav", "audio/wav", wav(pcm), token());
            if (res.optBoolean("ignored")) return;
            String heard = res.optString("heard", ""), reply = res.optString("reply", "");
            if (!heard.isEmpty()) ui.log("Você", heard);
            if (!reply.isEmpty()) ui.log("Kronus", reply);
            byte[] w = null;
            String b64 = res.optString("audio_b64", "");
            if (!b64.isEmpty() && !"null".equals(b64) && res.optString("audio_mime", "").contains("wav")) {
                try { w = Base64.decode(b64, Base64.DEFAULT); } catch (Exception ignored) { }
            }
            playInCall(w, res.optString("speech", reply));
        } catch (Exception e) {
            ui.state("⚠️ " + e.getMessage());
        } finally {
            listening = false;
            k.hangUp();
            ui.state("Chamada encerrada.");
        }
    }

    private String server() { return prefs.getString("server", ""); }
    private String token() { return prefs.getString("token", ""); }

    // ------------------------------------------------------------------ fala
    void playInCall(byte[] wav, String text) {
        if ("C1".equals(method())) {
            MainActivity.Pcm pcm = wav != null ? MainActivity.Pcm.fromWav(wav) : null;
            if (pcm == null && waitTts() && text != null && !text.isEmpty()) {
                byte[] f = ttsToWav(text);
                pcm = f == null ? null : MainActivity.Pcm.fromWav(f);
            }
            if (pcm != null && playVoip(pcm)) return;
        }
        speakDirect(text);
    }

    byte[] serverVoice(String text) {
        try {
            JSONObject body = new JSONObject();
            body.put("text", text);
            body.put("format", "wav");
            JSONObject res = http(server() + "/api/v1/speak", "application/json", body.toString().getBytes(StandardCharsets.UTF_8), token());
            String b64 = res.optString("audio_b64", "");
            return b64.isEmpty() || !res.optString("audio_mime", "").contains("wav") ? null : Base64.decode(b64, Base64.DEFAULT);
        } catch (Exception e) {
            return null;
        }
    }

    private boolean playVoip(MainActivity.Pcm pcm) {
        MainActivity.Pcm p16 = pcm.resample(RATE);
        byte[] data = p16.bytes(0.2, 0.4);
        int mask = AudioFormat.CHANNEL_OUT_MONO;
        int buf = Math.max(AudioTrack.getMinBufferSize(RATE, mask, AudioFormat.ENCODING_PCM_16BIT) * 2, RATE);
        AudioTrack track;
        try {
            track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setAudioFormat(new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(RATE).setChannelMask(mask).build())
                    .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(buf).build();
        } catch (Exception e) {
            return false;
        }
        try {
            if (track.getState() != AudioTrack.STATE_INITIALIZED) return false;
            track.play();
            int written = 0;
            while (written < data.length) {
                int w = track.write(data, written, Math.min(4096, data.length - written));
                if (w <= 0) break;
                written += w;
            }
            long frames = written / 2;
            long deadline = System.currentTimeMillis() + frames * 1000L / RATE + 3000;
            while (track.getPlaybackHeadPosition() < frames && System.currentTimeMillis() < deadline) sleep(50);
            return written > 0;
        } catch (Exception e) {
            return false;
        } finally {
            try { track.stop(); } catch (Exception ignored) { }
            track.release();
        }
    }

    void speakDirect(String text) {
        if (text == null || text.isEmpty() || !waitTts()) return;
        tts.setLanguage(new Locale("pt", "BR"));
        CountDownLatch done = new CountDownLatch(1);
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String id) { }
            @Override public void onDone(String id) { done.countDown(); }
            @Override public void onError(String id) { done.countDown(); }
        });
        Bundle params = new Bundle();
        params.putInt(TextToSpeech.Engine.KEY_PARAM_STREAM, AudioManager.STREAM_VOICE_CALL);
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, params, "aviso");
        try { done.await(60, TimeUnit.SECONDS); } catch (InterruptedException ignored) { }
    }

    private byte[] ttsToWav(String text) {
        try {
            tts.setLanguage(new Locale("pt", "BR"));
            java.io.File f = new java.io.File(ctx.getCacheDir(), "aviso.wav");
            if (f.exists()) f.delete();
            CountDownLatch done = new CountDownLatch(1);
            tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override public void onStart(String id) { }
                @Override public void onDone(String id) { done.countDown(); }
                @Override public void onError(String id) { done.countDown(); }
            });
            if (tts.synthesizeToFile(text, new Bundle(), f, "arquivo") != TextToSpeech.SUCCESS) return null;
            if (!done.await(30, TimeUnit.SECONDS) || !f.exists()) return null;
            sleep(150);
            byte[] data = new byte[(int) f.length()];
            try (java.io.FileInputStream in = new java.io.FileInputStream(f)) {
                int off = 0, n;
                while (off < data.length && (n = in.read(data, off, data.length - off)) > 0) off += n;
            }
            return data;
        } catch (Exception e) {
            return null;
        }
    }

    static void beeps(int n) {
        try {
            ToneGenerator tone = new ToneGenerator(AudioManager.STREAM_VOICE_CALL, 90);
            for (int i = 0; i < n; i++) {
                tone.startTone(ToneGenerator.TONE_PROP_BEEP, 180);
                sleep(380);
            }
            sleep(300);
            tone.release();
        } catch (Exception ignored) {
        }
    }

    // ------------------------------------------------------------------ escuta (mesma do app 1.10)
    byte[] record(long noSpeechMs, boolean strict) {
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec;
        try {
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, Math.max(min, RATE));
            if (rec.getState() != AudioRecord.STATE_INITIALIZED) return null;
        } catch (Exception e) {
            return null;                                   // sem microfone em segundo plano: só o aviso
        }
        MainActivity.disableVoiceEffects(rec.getAudioSessionId());
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        short[] buf = new short[RATE / 20];
        long start = System.currentTimeMillis(), lastVoice = 0;
        double floor = Double.MAX_VALUE;
        boolean heard = false;
        int loud = 0, speechStart = 0, need = strict ? 7 : 4;
        double factor = strict ? 4.5 : 3.5, minLevel = strict ? 450 : 260;
        rec.startRecording();
        try {
            while (listening) {
                int n = rec.read(buf, 0, buf.length);
                if (n <= 0) continue;
                double sum = 0;
                ByteBuffer bb = ByteBuffer.allocate(n * 2).order(ByteOrder.LITTLE_ENDIAN);
                for (int i = 0; i < n; i++) {
                    sum += (double) buf[i] * buf[i];
                    bb.putShort(buf[i]);
                }
                out.write(bb.array(), 0, n * 2);
                double rms = Math.sqrt(sum / n);
                long now = System.currentTimeMillis();
                if (now - start < 400) {
                    floor = Math.min(floor, rms);
                    continue;
                }
                if (rms > Math.max(minLevel, (floor == Double.MAX_VALUE ? 0 : floor) * factor)) {
                    if (++loud >= need && !heard) {
                        heard = true;
                        speechStart = Math.max(0, out.size() - need * n * 2 - RATE * 2 * 3 / 10);
                    }
                    lastVoice = now;
                } else {
                    loud = 0;
                }
                if (heard && now - lastVoice > 1600) break;
                if (!heard && now - start > noSpeechMs) break;
                if (now - start > 20000) break;
            }
        } finally {
            rec.stop();
            rec.release();
        }
        if (!heard) return null;
        byte[] all = out.toByteArray();
        return MainActivity.normalize(java.util.Arrays.copyOfRange(all, speechStart - speechStart % 2, all.length));
    }

    static byte[] wav(byte[] pcm) {
        ByteBuffer h = ByteBuffer.allocate(44).order(ByteOrder.LITTLE_ENDIAN);
        h.put("RIFF".getBytes(StandardCharsets.US_ASCII)).putInt(36 + pcm.length).put("WAVE".getBytes(StandardCharsets.US_ASCII));
        h.put("fmt ".getBytes(StandardCharsets.US_ASCII)).putInt(16).putShort((short) 1).putShort((short) 1)
                .putInt(RATE).putInt(RATE * 2).putShort((short) 2).putShort((short) 16);
        h.put("data".getBytes(StandardCharsets.US_ASCII)).putInt(pcm.length);
        byte[] out = new byte[44 + pcm.length];
        System.arraycopy(h.array(), 0, out, 0, 44);
        System.arraycopy(pcm, 0, out, 44, pcm.length);
        return out;
    }

    // ------------------------------------------------------------------ rede
    static JSONObject http(String url, String ctype, byte[] body, String token) throws Exception {
        if (!url.startsWith("https://")) throw new Exception("O endereço precisa começar com https://");
        HttpURLConnection con = (HttpURLConnection) new URL(url).openConnection();
        con.setConnectTimeout(20000);
        con.setReadTimeout(120000);
        con.setRequestProperty("User-Agent", "kronus-android/1.11");
        if (token != null && !token.isEmpty()) con.setRequestProperty("Authorization", "Bearer " + token);
        if (body == null) {
            con.setRequestMethod("GET");
        } else {
            con.setRequestMethod("POST");
            con.setDoOutput(true);
            con.setRequestProperty("Content-Type", ctype);
            con.setFixedLengthStreamingMode(body.length);
            try (OutputStream os = con.getOutputStream()) {
                os.write(body);
            }
        }
        int code = con.getResponseCode();
        InputStream is = code < 400 ? con.getInputStream() : con.getErrorStream();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        if (is != null) {
            byte[] b = new byte[8192];
            int n;
            while ((n = is.read(b)) > 0) out.write(b, 0, n);
            is.close();
        }
        String raw = out.toString("UTF-8");
        JSONObject json;
        try {
            json = new JSONObject(raw.isEmpty() ? "{}" : raw);
        } catch (Exception e) {
            json = new JSONObject();
        }
        if (code == 401) throw new Exception("Acesso recusado: este aparelho foi desconectado. Pareie de novo com /parear.");
        if (code >= 400) throw new Exception(json.optString("error", "erro HTTP " + code));
        return json;
    }
}
