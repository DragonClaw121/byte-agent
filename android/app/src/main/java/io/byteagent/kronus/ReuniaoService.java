package io.byteagent.kronus;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.IBinder;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.net.URLEncoder;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * App 2.1: grava uma reunião e manda pedaços de 20–30 s (cortados num silêncio) para o servidor, que transcreve na hora.
 * O texto volta na resposta de cada pedaço e aparece na tela enquanto a reunião acontece. Roda em primeiro plano (com
 * notificação "Gravando reunião"), então continua com a tela apagada. Ao parar, o servidor faz a ata.
 */
public class ReuniaoService extends Service {
    static final String ACTION_START = "io.byteagent.kronus.REUNIAO_INICIAR", ACTION_STOP = "io.byteagent.kronus.REUNIAO_PARAR";
    static final String CH = "kronus-reuniao";
    static final int ID = 5, RATE = 16000;
    static final int MIN_S = 20, MAX_S = 30;

    interface Listener {
        void update(String state, String newText);
    }

    static volatile Listener listener;
    static volatile boolean recording = false;
    static volatile int meetingId = -1;
    static volatile long startedAt = 0;
    static final StringBuilder live = new StringBuilder();
    static volatile String lastState = "";

    private volatile boolean on = false;
    private final LinkedBlockingQueue<Object[]> queue = new LinkedBlockingQueue<>();   // {seq, offset_s, wav}
    private Thread recorder, uploader;

    static void start(Context c, String title) {
        Intent i = new Intent(c, ReuniaoService.class).setAction(ACTION_START).putExtra("titulo", title);
        if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
        else c.startService(i);
    }

    static void stop(Context c) {
        c.startService(new Intent(c, ReuniaoService.class).setAction(ACTION_STOP));
    }

    private static void report(String state, String text) {
        lastState = state;
        if (text != null && !text.isEmpty()) {
            synchronized (live) {
                live.append(text).append("\n");
                if (live.length() > 20000) live.delete(0, live.length() - 20000);
            }
        }
        Listener l = listener;
        if (l != null) l.update(state, text);
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            on = false;                                  // o gravador termina o último pedaço e o enviador fecha a reunião
            report("Encerrando: enviando o final...", null);
            return START_NOT_STICKY;
        }
        if (recording) return START_NOT_STICKY;
        startForegroundNow("Preparando a gravação...");
        String title = intent == null ? "" : intent.getStringExtra("titulo");
        recording = true;
        on = true;
        synchronized (live) { live.setLength(0); }
        new Thread(() -> begin(title == null ? "" : title), "kronus-reuniao").start();
        return START_NOT_STICKY;
    }

    private void startForegroundNow(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CH, "Reuniões do Kronus", NotificationManager.IMPORTANCE_LOW));
        PendingIntent stop = PendingIntent.getService(this, 9, new Intent(this, ReuniaoService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Notification n = new Notification.Builder(this, CH)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle("🔴 Gravando reunião")
                .setContentText(text)
                .setOngoing(true)
                .setContentIntent(PendingIntent.getActivity(this, 8, new Intent(this, MainActivity.class), PendingIntent.FLAG_IMMUTABLE))
                .addAction(new Notification.Action.Builder(null, "Parar e gerar ata", stop).build())
                .build();
        if (Build.VERSION.SDK_INT >= 30) startForeground(ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);
        else startForeground(ID, n);
    }

    private void begin(String title) {
        SharedPreferences prefs = getSharedPreferences("kronus", MODE_PRIVATE);
        String server = prefs.getString("server", ""), token = prefs.getString("token", "");
        try {
            JSONObject body = new JSONObject();
            body.put("titulo", title);
            JSONObject res = Voz.http(server + "/api/v1/reuniao", "application/json", body.toString().getBytes(StandardCharsets.UTF_8), token);
            meetingId = res.getInt("id");
        } catch (Exception e) {
            report("⚠️ Não consegui começar: " + e.getMessage(), null);
            finishService();
            return;
        }
        startedAt = System.currentTimeMillis();
        report("🔴 Gravando reunião #" + meetingId, null);
        uploader = new Thread(() -> uploadLoop(server, token), "kronus-reuniao-envio");
        uploader.start();
        recorder = new Thread(this::recordLoop, "kronus-reuniao-gravador");
        recorder.start();
    }

    @SuppressWarnings("MissingPermission")
    private void recordLoop() {
        int minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec;
        try {
            rec = new AudioRecord(MediaRecorder.AudioSource.MIC, RATE, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, Math.max(minBuf, RATE));
            rec.startRecording();
        } catch (Exception e) {
            report("⚠️ Microfone indisponível: " + e.getMessage(), null);
            on = false;
            queue.add(new Object[]{-1, 0.0, null});
            return;
        }
        short[] buf = new short[RATE / 10];               // 100 ms
        ShortArray pcm = new ShortArray();
        int seq = 0;
        long samplesSent = 0;
        try {
            while (on) {
                int n = rec.read(buf, 0, buf.length);
                if (n <= 0) continue;
                pcm.add(buf, n);
                int secs = pcm.size / RATE;
                if (secs >= MIN_S) {
                    int cut = secs >= MAX_S ? pcm.size : quietCut(pcm, MIN_S * RATE);
                    if (cut > 0) {
                        queue.add(new Object[]{seq++, samplesSent / (double) RATE, wav(pcm.data, 0, cut)});
                        samplesSent += cut;
                        pcm.dropFirst(cut);
                        updateNotification();
                    }
                }
            }
        } finally {
            try { rec.stop(); } catch (Exception ignored) { }
            rec.release();
        }
        if (pcm.size > RATE) queue.add(new Object[]{seq, samplesSent / (double) RATE, wav(pcm.data, 0, pcm.size)});
        queue.add(new Object[]{-1, 0.0, null});           // fim: o enviador fecha a reunião
    }

    /** Procura, depois de `from`, um trecho de 300 ms bem baixo (pausa entre falas) para cortar sem partir palavras. */
    private static int quietCut(ShortArray pcm, int from) {
        int win = RATE * 3 / 10;
        double best = Double.MAX_VALUE;
        int bestAt = -1;
        for (int i = from; i + win <= pcm.size; i += RATE / 20) {
            double sum = 0;
            for (int j = i; j < i + win; j += 4) sum += Math.abs(pcm.data[j]);
            double avg = sum / (win / 4.0);
            if (avg < best) {
                best = avg;
                bestAt = i + win / 2;
            }
        }
        return best < 400 ? bestAt : -1;                  // sem pausa clara ainda: espera (até MAX_S)
    }

    private void uploadLoop(String server, String token) {
        while (true) {
            Object[] item;
            try {
                item = queue.poll(60, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                return;
            }
            if (item == null) continue;
            int seq = (int) item[0];
            if (seq < 0) break;
            String url = server + "/api/v1/reuniao/" + meetingId + "/audio?seq=" + seq + "&t=" + String.format(java.util.Locale.US, "%.1f", (double) item[1]);
            for (int attempt = 0; attempt < 6; attempt++) {
                try {
                    JSONObject res = Voz.http(url, "audio/wav", (byte[]) item[2], token);
                    if (res.has("error")) throw new Exception(res.optString("error"));
                    report("🔴 Gravando reunião #" + meetingId, res.optString("texto", ""));
                    break;
                } catch (Exception e) {
                    report("⚠️ Sem rede, tentando de novo... (" + e.getMessage() + ")", null);
                    Voz.sleep(Math.min(30000, 3000L * (attempt + 1)));
                }
            }
        }
        try {
            Voz.http(server + "/api/v1/reuniao/" + meetingId + "/fim", "application/json", "{}".getBytes(StandardCharsets.UTF_8), token);
            report("📝 Gravação encerrada. A ata chega na conversa em alguns instantes.", null);
            Conversa.add(this, Conversa.AVISO, "📝 Reunião #" + meetingId + " encerrada: preparando a ata...", null);
        } catch (Exception e) {
            report("⚠️ Não consegui encerrar: " + e.getMessage() + " (mande /ata " + meetingId + " depois)", null);
        }
        finishService();
    }

    private void updateNotification() {
        long s = (System.currentTimeMillis() - startedAt) / 1000;
        NotificationManager nm = getSystemService(NotificationManager.class);
        PendingIntent stop = PendingIntent.getService(this, 9, new Intent(this, ReuniaoService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        nm.notify(ID, new Notification.Builder(this, CH)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle("🔴 Gravando reunião")
                .setContentText(String.format(java.util.Locale.US, "%02d:%02d gravados", s / 60, s % 60))
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "Parar e gerar ata", stop).build())
                .build());
    }

    private void finishService() {
        recording = false;
        stopForeground(true);
        stopSelf();
    }

    static byte[] wav(short[] samples, int from, int to) {
        int n = to - from;
        ByteBuffer b = ByteBuffer.allocate(44 + n * 2).order(ByteOrder.LITTLE_ENDIAN);
        b.put("RIFF".getBytes(StandardCharsets.US_ASCII)).putInt(36 + n * 2).put("WAVE".getBytes(StandardCharsets.US_ASCII));
        b.put("fmt ".getBytes(StandardCharsets.US_ASCII)).putInt(16).putShort((short) 1).putShort((short) 1)
                .putInt(RATE).putInt(RATE * 2).putShort((short) 2).putShort((short) 16);
        b.put("data".getBytes(StandardCharsets.US_ASCII)).putInt(n * 2);
        for (int i = from; i < to; i++) b.putShort(samples[i]);
        return b.array();
    }

    /** Envia um arquivo gravado (até 150 MB) para virar transcrição e ata. Chame fora da thread da tela. */
    static String uploadFile(Context c, android.net.Uri uri, String title) {
        SharedPreferences prefs = c.getSharedPreferences("kronus", MODE_PRIVATE);
        String server = prefs.getString("server", ""), token = prefs.getString("token", "");
        try (java.io.InputStream in = c.getContentResolver().openInputStream(uri)) {
            if (in == null) return "⚠️ Não consegui abrir o arquivo.";
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] chunk = new byte[64 * 1024];
            int n;
            while ((n = in.read(chunk)) > 0) {
                out.write(chunk, 0, n);
                if (out.size() > 150 * 1024 * 1024) return "⚠️ Arquivo acima de 150 MB.";
            }
            String type = c.getContentResolver().getType(uri);
            String name = uri.getLastPathSegment() == null ? "reuniao" : uri.getLastPathSegment();
            JSONObject res = Voz.http(server + "/api/v1/reuniao/arquivo?nome=" + URLEncoder.encode(name, "UTF-8")
                    + "&titulo=" + URLEncoder.encode(title == null ? "" : title, "UTF-8"),
                    type == null ? "application/octet-stream" : type, out.toByteArray(), token);
            if (res.has("error")) return "⚠️ " + res.optString("error");
            return "📝 Recebi \"" + res.optString("title") + "\" (#" + res.optInt("id") + "). A ata chega na conversa quando ficar pronta.";
        } catch (Exception e) {
            return "⚠️ " + e.getMessage();
        }
    }

    /** Lista de amostras que cresce (sem bibliotecas). */
    static final class ShortArray {
        short[] data = new short[RATE * 40];
        int size = 0;

        void add(short[] src, int n) {
            if (size + n > data.length) data = java.util.Arrays.copyOf(data, Math.max(data.length * 2, size + n));
            System.arraycopy(src, 0, data, size, n);
            size += n;
        }

        void dropFirst(int n) {
            System.arraycopy(data, n, data, 0, size - n);
            size -= n;
        }
    }
}
