package io.byteagent.kronus;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.MediaPlayer;
import android.media.MediaRecorder;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.util.Base64;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
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
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Kronus no Android: aperta (ou chama pelo botão de assistente do relógio), fala, ouve a resposta.
 * Com "microfone do relógio" ligado, a conversa vai pelo áudio Bluetooth do relógio, como numa ligação.
 * Sem bibliotecas externas: só APIs do Android.
 */
public class MainActivity extends Activity {
    private static final int RATE = 16000;
    private static final int REQ_PERMS = 7;
    private static final Pattern LINK = Pattern.compile("(https://[A-Za-z0-9.\\-]+(?::\\d+)?)/voz#parear=(\\d{6})");
    private static final Pattern CODE = Pattern.compile("^\\s*(\\d{6})\\s*$");

    private SharedPreferences prefs;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private TextView title, state, log;
    private Button mic, disconnect;
    private CheckBox useWatch;
    private LinearLayout setup;
    private EditText input;
    private TextToSpeech tts;
    private volatile boolean ttsReady = false;
    private volatile boolean listening = false, busy = false;
    private boolean autoListen = false;

    // ------------------------------------------------------------------ tela
    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        prefs = getSharedPreferences("kronus", Context.MODE_PRIVATE);
        buildUi();
        tts = new TextToSpeech(this, status -> ttsReady = status == TextToSpeech.SUCCESS);
        autoListen = isVoiceIntent(getIntent());
        refresh();
        askPermissions();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (isVoiceIntent(intent)) {
            autoListen = true;
            maybeAutoListen();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        maybeAutoListen();
    }

    @Override
    protected void onDestroy() {
        listening = false;
        if (tts != null) tts.shutdown();
        super.onDestroy();
    }

    private static boolean isVoiceIntent(Intent i) {
        if (i == null || i.getAction() == null) return false;
        String a = i.getAction();
        return a.equals(Intent.ACTION_ASSIST) || a.equals(Intent.ACTION_VOICE_COMMAND) || a.equals(Intent.ACTION_SEARCH_LONG_PRESS);
    }

    private void maybeAutoListen() {
        if (autoListen && paired() && hasMic() && !busy) {
            autoListen = false;
            ui.postDelayed(this::startListening, 300);
        }
    }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private void buildUi() {
        int bg = Color.rgb(11, 15, 23), ink = Color.rgb(238, 242, 247), muted = Color.rgb(151, 161, 178), blue = Color.rgb(37, 99, 235);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(bg);
        root.setPadding(dp(16), dp(16), dp(16), dp(16));
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int top, bottom;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets s = insets.getInsets(WindowInsets.Type.systemBars());
                top = s.top; bottom = s.bottom;
            } else {
                top = insets.getSystemWindowInsetTop(); bottom = insets.getSystemWindowInsetBottom();
            }
            v.setPadding(dp(16), top + dp(12), dp(16), bottom + dp(12));
            return insets;
        });

        title = new TextView(this);
        title.setText("Kronus");
        title.setTextColor(ink);
        title.setTextSize(22);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(title);

        setup = new LinearLayout(this);
        setup.setOrientation(LinearLayout.VERTICAL);
        setup.setPadding(0, dp(16), 0, dp(8));
        TextView help = new TextView(this);
        help.setText("Para conectar: mande /parear no Telegram do Kronus, segure o link que ele enviar e toque em Copiar. "
                + "Depois toque em \"Colar do Telegram\".");
        help.setTextColor(muted);
        help.setTextSize(15);
        setup.addView(help);
        input = new EditText(this);
        input.setHint("link do /parear (ou código, se já conectou antes)");
        input.setTextColor(ink);
        input.setHintTextColor(muted);
        input.setSingleLine(true);
        setup.addView(input);
        Button paste = new Button(this);
        paste.setText("Colar do Telegram");
        paste.setOnClickListener(v -> pasteFromClipboard());
        setup.addView(paste);
        Button connect = new Button(this);
        connect.setText("Conectar");
        connect.setOnClickListener(v -> pair(input.getText().toString()));
        setup.addView(connect);
        root.addView(setup);

        mic = new Button(this);
        mic.setText("🎙️");
        mic.setTextSize(48);
        mic.setTextColor(Color.WHITE);
        GradientDrawable circle = new GradientDrawable();
        circle.setShape(GradientDrawable.OVAL);
        circle.setColor(blue);
        mic.setBackground(circle);
        LinearLayout.LayoutParams micParams = new LinearLayout.LayoutParams(dp(180), dp(180));
        micParams.gravity = Gravity.CENTER_HORIZONTAL;
        micParams.topMargin = dp(36);
        mic.setLayoutParams(micParams);
        mic.setOnClickListener(v -> {
            if (listening) listening = false;          // segundo toque: termina de ouvir e envia
            else startListening();
        });
        root.addView(mic);

        state = new TextView(this);
        state.setTextColor(muted);
        state.setTextSize(16);
        state.setGravity(Gravity.CENTER_HORIZONTAL);
        state.setPadding(0, dp(14), 0, dp(6));
        root.addView(state);

        useWatch = new CheckBox(this);
        useWatch.setText("Usar microfone e alto-falante do relógio (Bluetooth)");
        useWatch.setTextColor(muted);
        useWatch.setChecked(prefs.getBoolean("watch", true));
        useWatch.setOnCheckedChangeListener((b, on) -> prefs.edit().putBoolean("watch", on).apply());
        root.addView(useWatch);

        ScrollView scroll = new ScrollView(this);
        log = new TextView(this);
        log.setTextColor(ink);
        log.setTextSize(15);
        log.setPadding(0, dp(8), 0, dp(8));
        scroll.addView(log);
        root.addView(scroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        disconnect = new Button(this);
        disconnect.setText("Desconectar este aparelho");
        disconnect.setOnClickListener(v -> {
            prefs.edit().remove("token").apply();
            say("Desconectado. Para remover o acesso de vez, mande /desparear no Telegram.");
            refresh();
        });
        root.addView(disconnect);
        setContentView(root);
    }

    private boolean paired() {
        return !prefs.getString("token", "").isEmpty() && !prefs.getString("server", "").isEmpty();
    }

    private void refresh() {
        boolean ok = paired();
        setup.setVisibility(ok ? View.GONE : View.VISIBLE);
        mic.setVisibility(ok ? View.VISIBLE : View.GONE);
        useWatch.setVisibility(ok ? View.VISIBLE : View.GONE);
        disconnect.setVisibility(ok ? View.VISIBLE : View.GONE);
        title.setText(prefs.getString("agent", "Kronus"));
        if (ok) say("Toque para falar.");
    }

    private void say(String text) {
        ui.post(() -> state.setText(text));
    }

    private void addLog(String who, String text) {
        ui.post(() -> {
            String prev = log.getText().toString();
            String next = (prev.isEmpty() ? "" : prev + "\n\n") + who + ": " + text;
            if (next.length() > 6000) next = next.substring(next.length() - 6000);
            log.setText(next);
        });
    }

    // ------------------------------------------------------------------ permissões e pareamento
    private boolean hasMic() {
        return checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED;
    }

    private void askPermissions() {
        java.util.ArrayList<String> need = new java.util.ArrayList<>();
        if (!hasMic()) need.add(Manifest.permission.RECORD_AUDIO);
        if (Build.VERSION.SDK_INT >= 31 && checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED)
            need.add(Manifest.permission.BLUETOOTH_CONNECT);
        if (!need.isEmpty()) requestPermissions(need.toArray(new String[0]), REQ_PERMS);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        super.onRequestPermissionsResult(code, perms, results);
        if (!hasMic()) say("Sem permissão de microfone: libere em Configurações > Apps > Kronus.");
        else maybeAutoListen();
    }

    private void pasteFromClipboard() {
        ClipboardManager cm = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        ClipData clip = cm == null ? null : cm.getPrimaryClip();
        if (clip != null && clip.getItemCount() > 0 && clip.getItemAt(0).getText() != null) {
            input.setText(clip.getItemAt(0).getText().toString());
            pair(input.getText().toString());
        } else {
            say("A área de transferência está vazia. Copie o link do /parear no Telegram.");
        }
    }

    private void pair(String text) {
        String server, code;
        Matcher m = LINK.matcher(text == null ? "" : text);
        Matcher c = CODE.matcher(text == null ? "" : text);
        if (m.find()) {
            server = m.group(1);
            code = m.group(2);
        } else if (c.find() && !prefs.getString("server", "").isEmpty()) {
            server = prefs.getString("server", "");
            code = c.group(1);
        } else {
            say("Cole o link inteiro que o /parear enviou (https://.../voz#parear=123456).");
            return;
        }
        say("Conectando...");
        final String srv = server, cd = code;
        new Thread(() -> {
            try {
                JSONObject body = new JSONObject();
                body.put("code", cd);
                body.put("device", "Android app (" + Build.MANUFACTURER + " " + Build.MODEL + ")");
                JSONObject res = http(srv + "/api/v1/pair", "application/json", body.toString().getBytes(StandardCharsets.UTF_8), null);
                prefs.edit().putString("server", srv).putString("token", res.getString("token"))
                        .putString("agent", res.optString("agent", "Kronus")).apply();
                ui.post(() -> {
                    input.setText("");
                    refresh();
                });
            } catch (Exception e) {
                say("⚠️ " + e.getMessage());
            }
        }).start();
    }

    // ------------------------------------------------------------------ áudio do relógio (Bluetooth)
    private boolean routeToWatch(AudioManager am) {
        if (!prefs.getBoolean("watch", true)) return false;
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                for (AudioDeviceInfo d : am.getAvailableCommunicationDevices()) {
                    if (d.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) {
                        am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                        if (am.setCommunicationDevice(d)) {
                            Thread.sleep(700);          // o relógio leva um instante para abrir o canal de voz
                            return true;
                        }
                    }
                }
                return false;
            }
            if (!am.isBluetoothScoAvailableOffCall()) return false;
            am.setMode(AudioManager.MODE_IN_COMMUNICATION);
            am.startBluetoothSco();
            am.setBluetoothScoOn(true);
            for (int i = 0; i < 30 && !am.isBluetoothScoOn(); i++) Thread.sleep(100);
            return am.isBluetoothScoOn();
        } catch (Exception e) {
            return false;
        }
    }

    private void releaseWatch(AudioManager am, boolean routed) {
        try {
            if (routed) {
                if (Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice();
                else {
                    am.setBluetoothScoOn(false);
                    am.stopBluetoothSco();
                }
            }
            am.setMode(AudioManager.MODE_NORMAL);
        } catch (Exception ignored) {
        }
    }

    // ------------------------------------------------------------------ ouvir, enviar, responder
    private void startListening() {
        if (busy || !paired()) return;
        if (!hasMic()) {
            askPermissions();
            return;
        }
        busy = true;
        listening = true;
        ui.post(() -> mic.setText("⏹️"));
        new Thread(this::converse).start();
    }

    private void converse() {
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        boolean watch = routeToWatch(am);
        try {
            say(watch ? "Ouvindo pelo relógio..." : "Ouvindo...");
            byte[] pcm = record(watch);
            ui.post(() -> mic.setText("🎙️"));
            if (pcm == null) {
                say("Não ouvi nada. Toque para falar.");
                return;
            }
            say("Pensando...");
            JSONObject res = http(prefs.getString("server", "") + "/api/v1/voice", "audio/wav", wav(pcm), prefs.getString("token", ""));
            if (res.optBoolean("ignored")) {
                say("Não entendi. Toque para tentar de novo.");
                return;
            }
            String heard = res.optString("heard", "");
            String reply = res.optString("reply", "");
            if (!heard.isEmpty()) addLog("Você", heard);
            if (!reply.isEmpty()) addLog(prefs.getString("agent", "Kronus"), reply);
            say("Respondendo...");
            speak(res, watch);
            say("Toque para falar.");
        } catch (Exception e) {
            say("⚠️ " + e.getMessage());
        } finally {
            releaseWatch(am, watch);
            listening = false;
            busy = false;
            ui.post(() -> mic.setText("🎙️"));
        }
    }

    /** Grava até a pessoa parar de falar (1,3 s de silêncio), no máximo 15 s. Devolve null se não houve fala. */
    private byte[] record(boolean watch) {
        int source = watch ? MediaRecorder.AudioSource.VOICE_COMMUNICATION : MediaRecorder.AudioSource.VOICE_RECOGNITION;
        int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        AudioRecord rec;
        try {
            rec = new AudioRecord(source, RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, RATE));
        } catch (SecurityException e) {
            return null;
        }
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        short[] buf = new short[RATE / 20];               // blocos de 50 ms
        long start = System.currentTimeMillis(), lastVoice = 0;
        double floor = -1;
        boolean heard = false;
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
                if (now - start < 300) {                    // primeiros 300 ms: mede o ruído do ambiente
                    floor = floor < 0 ? rms : (floor + rms) / 2;
                    continue;
                }
                double threshold = Math.max(450, floor * 2.5);
                if (rms > threshold) {
                    heard = true;
                    lastVoice = now;
                }
                if (heard && now - lastVoice > 1300) break;
                if (!heard && now - start > 7000) break;
                if (now - start > 15000) break;
            }
        } finally {
            rec.stop();
            rec.release();
        }
        return heard ? out.toByteArray() : null;
    }

    private static byte[] wav(byte[] pcm) {
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

    /** Toca a voz do servidor (a mesma do Telegram); se não der, usa a voz do Android. */
    private void speak(JSONObject res, boolean watch) {
        String b64 = res.optString("audio_b64", "");
        String text = res.optString("speech", res.optString("reply", ""));
        if (!b64.isEmpty() && !"null".equals(b64)) {
            try {
                String mime = res.optString("audio_mime", "audio/ogg");
                File f = new File(getCacheDir(), mime.contains("mpeg") ? "resposta.mp3" : "resposta.ogg");
                try (FileOutputStream fo = new FileOutputStream(f)) {
                    fo.write(Base64.decode(b64, Base64.DEFAULT));
                }
                MediaPlayer mp = new MediaPlayer();
                mp.setAudioAttributes(new AudioAttributes.Builder()
                        .setUsage(watch ? AudioAttributes.USAGE_VOICE_COMMUNICATION : AudioAttributes.USAGE_ASSISTANT)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build());
                mp.setDataSource(f.getAbsolutePath());
                mp.prepare();
                CountDownLatch done = new CountDownLatch(1);
                mp.setOnCompletionListener(p -> done.countDown());
                mp.setOnErrorListener((p, w, x) -> { done.countDown(); return true; });
                mp.start();
                done.await(90, TimeUnit.SECONDS);
                mp.release();
                return;
            } catch (Exception ignored) {
                // cai para a voz do Android
            }
        }
        if (text.isEmpty() || !ttsReady) return;
        tts.setLanguage(new Locale("pt", "BR"));
        CountDownLatch done = new CountDownLatch(1);
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String id) { }
            @Override public void onDone(String id) { done.countDown(); }
            @Override public void onError(String id) { done.countDown(); }
        });
        Bundle params = new Bundle();
        params.putInt(TextToSpeech.Engine.KEY_PARAM_STREAM, watch ? AudioManager.STREAM_VOICE_CALL : AudioManager.STREAM_MUSIC);
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, params, "kronus");
        try {
            done.await(90, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
        }
    }

    // ------------------------------------------------------------------ rede
    private static JSONObject http(String url, String ctype, byte[] body, String token) throws Exception {
        if (!url.startsWith("https://")) throw new Exception("O endereço precisa começar com https://");
        HttpURLConnection con = (HttpURLConnection) new URL(url).openConnection();
        con.setConnectTimeout(20000);
        con.setReadTimeout(120000);
        con.setRequestMethod("POST");
        con.setDoOutput(true);
        con.setRequestProperty("Content-Type", ctype);
        con.setRequestProperty("User-Agent", "kronus-android/1.0");
        if (token != null && !token.isEmpty()) con.setRequestProperty("Authorization", "Bearer " + token);
        con.setFixedLengthStreamingMode(body.length);
        try (OutputStream os = con.getOutputStream()) {
            os.write(body);
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
