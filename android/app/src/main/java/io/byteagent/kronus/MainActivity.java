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
import android.media.AudioTrack;
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
    static final String ACTION_ANSWER = "io.byteagent.kronus.ATENDER";
    private static final Pattern LINK = Pattern.compile("(https://[A-Za-z0-9.\\-]+(?::\\d+)?)/voz#parear=(\\d{6})");
    private static final Pattern CODE = Pattern.compile("^\\s*(\\d{6})\\s*$");

    private SharedPreferences prefs;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private TextView title, conn, state, log, diag;
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
        try {
            KronusCallService.register(this);              // v1.8: o Kronus como "app de chamadas"
        } catch (Exception e) {
            prefs.edit().putString("call_error", e.getClass().getSimpleName()).apply();
        }
        autoListen = isVoiceIntent(getIntent());
        refresh();
        askPermissions();
        handleAnswer(getIntent());
    }

    /** Chamada do Kronus atendida pela notificação: aparece sobre a tela de bloqueio SÓ nesse caso. */
    private void handleAnswer(Intent intent) {
        if (intent == null || !ACTION_ANSWER.equals(intent.getAction())) {
            if (Build.VERSION.SDK_INT >= 27) setShowWhenLocked(false);
            return;
        }
        if (Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true);
            setTurnScreenOn(true);
        }
        say("Atendendo o Kronus...");
        KronusCallService.answerIncoming();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleAnswer(intent);
        if (isVoiceIntent(intent)) {
            autoListen = true;
            maybeAutoListen();
        }
    }

    @Override
    protected void onPause() {
        KronusCallService.uiListener = null;
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        KronusCallService.uiListener = new Voz.Ui() {
            @Override public void state(String text) { say(text); }
            @Override public void log(String who, String text) { addLog(who, text); }
        };
        if (paired() && prefs.getBoolean("ligar", false) && !KronusWatchService.isRunning()) {
            try { KronusWatchService.start(this); } catch (Exception ignored) { }
        }
        checkStatus();
        maybeAutoListen();
    }

    /** Testa de verdade: o Kronus responde? o acesso ainda vale? o relógio está disponível? */
    private void checkStatus() {
        if (!paired()) {
            ui.post(() -> conn.setText("⚪ Não conectado"));
            return;
        }
        ui.post(() -> conn.setText("… verificando a conexão"));
        new Thread(() -> {
            String server, watch = watchAvailability();
            try {
                JSONObject st = http(prefs.getString("server", "") + "/api/v1/status", null, null, prefs.getString("token", ""));
                server = st.optBoolean("online", true)
                        ? "🟢 " + st.optString("agent", "Kronus") + " online (v" + st.optString("version", "?") + ")"
                        : "🟡 " + st.optString("agent", "Kronus") + " no ar, mas o modelo está offline";
            } catch (Exception e) {
                String msg = String.valueOf(e.getMessage());
                if (msg.startsWith("Acesso recusado")) {
                    prefs.edit().remove("token").apply();
                    ui.post(this::refresh);
                    server = "🔴 Acesso removido: pareie de novo com /parear";
                } else {
                    server = "🔴 Sem conexão com o Kronus (" + msg + ")";
                }
            }
            String text = server + "\n" + watch + "\n🔊 Som pelo canal de ligação, método " + method();
            ui.post(() -> conn.setText(text));
        }).start();
    }

    private String watchAvailability() {
        if (!prefs.getBoolean("watch", true)) return "📱 Usando o microfone do celular";
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        if (Build.VERSION.SDK_INT >= 31) {
            try {
                for (AudioDeviceInfo d : am.getAvailableCommunicationDevices())
                    if (d.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) return "⌚ Relógio disponível: " + d.getProductName();
            } catch (Exception ignored) {
            }
            return "⌚ Relógio não encontrado (confira se está conectado ao celular)";
        }
        return am.isBluetoothScoAvailableOffCall() ? "⌚ Bluetooth de voz disponível" : "⌚ Relógio não encontrado";
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
        conn = new TextView(this);
        conn.setTextColor(muted);
        conn.setTextSize(13);
        root.addView(conn);

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
            if (listening) listening = false;          // tocar enquanto ouve: termina de ouvir e envia
            else if (KronusCallService.current != null) {
                KronusCallService.end();               // tocar durante a chamada (fora da escuta): desliga
                say("Chamada encerrada.");
            } else startListening();
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

        TextView pick = new TextView(this);
        pick.setText("Método de som no relógio (use o que o teste mostrar que funciona):");
        pick.setTextColor(muted);
        root.addView(pick);
        android.widget.RadioGroup methods = new android.widget.RadioGroup(this);
        methods.setOrientation(LinearLayout.HORIZONTAL);
        for (int i = 0; i < METHODS.length; i++) {
            android.widget.RadioButton rb = new android.widget.RadioButton(this);
            rb.setId(1000 + i);
            rb.setText(METHODS[i]);
            rb.setTextColor(ink);
            methods.addView(rb);
            if (METHODS[i].equals(method())) rb.setChecked(true);
        }
        methods.setOnCheckedChangeListener((g, id) -> {
            prefs.edit().putString("metodo", METHODS[id - 1000]).apply();
            checkStatus();
        });
        root.addView(methods);

        diag = new TextView(this);
        diag.setTextColor(muted);
        diag.setTextSize(13);
        root.addView(diag);

        CheckBox follow = new CheckBox(this);
        follow.setText("Continuar a conversa depois da resposta (escuta de novo)");
        follow.setTextColor(muted);
        follow.setChecked(prefs.getBoolean("seguir", false));
        follow.setOnCheckedChangeListener((b, on) -> prefs.edit().putBoolean("seguir", on).apply());
        root.addView(follow);

        CheckBox calls = new CheckBox(this);
        calls.setText("📞 Kronus pode me ligar para avisar (lembretes e compromissos)");
        calls.setTextColor(ink);
        calls.setChecked(prefs.getBoolean("ligar", false));
        LinearLayout callOpts = new LinearLayout(this);
        callOpts.setOrientation(LinearLayout.VERTICAL);
        callOpts.setPadding(dp(24), 0, 0, 0);
        String[][] kinds = {{"ligar_lembretes", "Lembretes", "1"}, {"ligar_agenda", "Compromissos chegando (agenda)", "1"},
                {"ligar_outros", "Outros avisos (objetivos, tarefas paradas...)", "0"}};
        for (String[] kind : kinds) {
            CheckBox cb = new CheckBox(this);
            cb.setText(kind[1]);
            cb.setTextColor(muted);
            cb.setChecked(prefs.getBoolean(kind[0], "1".equals(kind[2])));
            cb.setOnCheckedChangeListener((b, on) -> prefs.edit().putBoolean(kind[0], on).apply());
            callOpts.addView(cb);
        }
        callOpts.setVisibility(calls.isChecked() ? View.VISIBLE : View.GONE);
        calls.setOnCheckedChangeListener((b, on) -> {
            prefs.edit().putBoolean("ligar", on).apply();
            callOpts.setVisibility(on ? View.VISIBLE : View.GONE);
            if (on) {
                if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_PERMS);
                KronusWatchService.start(this);
                say("Kronus atento: lembretes vão tocar como chamada.");
            } else {
                KronusWatchService.stop(this);
                say("O Kronus não vai mais ligar; os avisos seguem no Telegram.");
            }
        });
        root.addView(calls);
        root.addView(callOpts);

        Button ringTest = new Button(this);
        ringTest.setText("Testar: Kronus me liga (em 5 s)");
        ringTest.setOnClickListener(v -> {
            say("Em 5 segundos o Kronus vai ligar. Pode bloquear o celular.");
            ui.postDelayed(() -> {
                if (!KronusCallService.ring(this, "Isto é um teste do Kronus te liga. Se você atendeu pelo relógio, deu certo. "
                        + "Pode me responder depois do bipe."))
                    say("⚠️ Não consegui ligar: " + KronusCallService.lastError);
            }, 5000);
        });
        root.addView(ringTest);

        Button replay = new Button(this);
        replay.setText("Ouvir minha última gravação");
        replay.setOnClickListener(v -> replayLast());
        root.addView(replay);

        Button test = new Button(this);
        test.setText("Testar som do relógio");
        test.setOnClickListener(v -> testWatch());
        root.addView(test);

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

    /** Toca no celular o que o app enviou da última vez: mostra se o problema é o áudio do relógio ou a transcrição. */
    private void replayLast() {
        byte[] pcm = lastRecording;
        if (pcm == null || busy) {
            say(pcm == null ? "Ainda não há gravação: fale com o Kronus primeiro." : "Aguarde terminar.");
            return;
        }
        new Thread(() -> {
            say(String.format(Locale.US, "Tocando sua última gravação (%.1f s)...", pcm.length / (double) (RATE * 2)));
            playTrack(new Pcm(toShorts(pcm), RATE), false, false);
            say("Toque para falar.");
        }).start();
    }

    private static short[] toShorts(byte[] pcm) {
        short[] out = new short[pcm.length / 2];
        ByteBuffer.wrap(pcm).order(ByteOrder.LITTLE_ENDIAN).asShortBuffer().get(out);
        return out;
    }

    private void say(String text) {
        ui.post(() -> state.setText(text));
    }

    private void info(String text) {
        ui.post(() -> diag.setText(text));
    }

    /** Teste dentro de uma chamada: 1 bipe = C1 (voz do Kronus pelo app), 2 bipes = C2 (voz do Android). */
    private void testWatch() {
        if (busy) return;
        busy = true;
        new Thread(() -> {
            StringBuilder r = new StringBuilder();
            KronusCallService.KronusConnection k = KronusCallService.start(this, 6000);
            try {
                if (k == null) {
                    r.append("📞 A chamada não começou: ").append(KronusCallService.lastError);
                    info(r.toString());
                    return;
                }
                boolean bt = k.waitBluetooth(4000);
                sleep(600);
                r.append(bt ? "📞 Em chamada pelo relógio" + (k.routeName.isEmpty() ? "" : " (" + k.routeName + ")")
                        : "📞 Em chamada, mas o áudio ficou no celular (o relógio não entrou)");
                info(r.toString());
                String[] test = {"C1", "C2"};
                String saved = prefs.getString("metodo", "C2");
                for (int i = 0; i < test.length && !k.ended; i++) {
                    beeps(i + 1);
                    playInfo = "";
                    prefs.edit().putString("metodo", test[i]).apply();
                    String phrase = "Método " + test[i] + ". Esta é uma frase de teste mais longa, com pausas, vírgulas e "
                            + "pontos. Se você me ouviu inteira no relógio, escolha o método " + test[i] + ".";
                    byte[] sample = "C1".equals(test[i]) ? serverVoice(phrase) : null;   // C1: a voz do Kronus de verdade
                    playInCall(sample, phrase);
                    r.append("\n").append(i + 1).append(i == 0 ? " bipe" : " bipes").append(" → ").append(test[i]).append(": ")
                            .append(playInfo.isEmpty() ? "voz do Android" : playInfo);
                    info(r.toString());
                    sleep(500);
                }
                prefs.edit().putString("metodo", saved).apply();
                r.append("\nEscolha abaixo o que saiu inteiro no relógio.");
                info(r.toString());
            } catch (Exception e) {
                info(r + "\nerro no teste: " + e.getMessage());
            } finally {
                if (k != null) k.hangUp();
                busy = false;
            }
        }).start();
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
                    checkStatus();
                });
            } catch (Exception e) {
                say("⚠️ " + e.getMessage());
            }
        }).start();
    }

    // ------------------------------------------------------------------ áudio do relógio (Bluetooth)
    /** Espera o Android confirmar que o canal de voz Bluetooth abriu (o tempo varia de relógio para relógio). */
    private boolean waitScoConnected(AudioManager am, long timeoutMs) {
        final CountDownLatch up = new CountDownLatch(1);
        android.content.BroadcastReceiver r = new android.content.BroadcastReceiver() {
            @Override
            public void onReceive(Context c, Intent i) {
                if (i.getIntExtra(AudioManager.EXTRA_SCO_AUDIO_STATE, -1) == AudioManager.SCO_AUDIO_STATE_CONNECTED) up.countDown();
            }
        };
        android.content.IntentFilter f = new android.content.IntentFilter(AudioManager.ACTION_SCO_AUDIO_STATE_UPDATED);
        try {
            if (Build.VERSION.SDK_INT >= 33) registerReceiver(r, f, Context.RECEIVER_EXPORTED);
            else registerReceiver(r, f);
            long end = System.currentTimeMillis() + timeoutMs;
            while (System.currentTimeMillis() < end) {
                if (up.await(100, TimeUnit.MILLISECONDS) || am.isBluetoothScoOn() || scoActive(am)) return true;
            }
            return false;
        } catch (Exception e) {
            return false;
        } finally {
            try { unregisterReceiver(r); } catch (Exception ignored) { }
        }
    }

    private static boolean scoActive(AudioManager am) {
        if (Build.VERSION.SDK_INT < 31) return am.isBluetoothScoOn();
        AudioDeviceInfo d = am.getCommunicationDevice();
        return d != null && d.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO;
    }

    /** Antes de tocar a resposta: o relógio pode ter fechado o canal enquanto o Kronus pensava. Reabre se preciso. */
    private boolean ensureWatch(AudioManager am) {
        if (scoActive(am) && am.getMode() == AudioManager.MODE_IN_COMMUNICATION) {
            sleep(300);
            return true;
        }
        return routeToWatch(am) != null;
    }

    private static void sleep(long ms) {
        try { Thread.sleep(ms); } catch (InterruptedException ignored) { }
    }

    /** Liga o áudio do relógio (como numa ligação). Devolve o nome do aparelho, ou null (e explica em routeInfo). */
    private String routeInfo = "";

    private String routeToWatch(AudioManager am) {
        if (!prefs.getBoolean("watch", true)) {
            routeInfo = "📱 Áudio: celular (opção do relógio desligada)";
            return null;
        }
        StringBuilder seen = new StringBuilder();
        try {
            if (Build.VERSION.SDK_INT >= 31) {
                for (AudioDeviceInfo d : am.getAvailableCommunicationDevices()) {
                    seen.append(seen.length() == 0 ? "" : ", ").append(d.getProductName()).append(" (tipo ").append(d.getType()).append(")");
                    if (d.getType() == AudioDeviceInfo.TYPE_BLUETOOTH_SCO) {
                        am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                        if (am.setCommunicationDevice(d)) {
                            boolean open = waitScoConnected(am, 4000);
                            sleep(450);                 // o começo do som costuma ser cortado logo que o canal abre
                            routeInfo = "⌚ Áudio: " + d.getProductName() + (open ? " (Bluetooth)" : " (Bluetooth, sem confirmação)");
                            return String.valueOf(d.getProductName());
                        }
                    }
                }
            }
            // método antigo (Android 11 ou menos, e plano B em celulares que não listam o relógio acima)
            if (am.isBluetoothScoAvailableOffCall()) {
                am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                am.startBluetoothSco();
                am.setBluetoothScoOn(true);
                if (waitScoConnected(am, 4000)) {
                    sleep(450);
                    routeInfo = "⌚ Áudio: relógio (Bluetooth, modo antigo)";
                    return "relógio";
                }
                am.stopBluetoothSco();
            }
        } catch (Exception e) {
            seen.append(" [erro: ").append(e.getClass().getSimpleName()).append("]");
        }
        am.setMode(AudioManager.MODE_NORMAL);
        routeInfo = "📱 Áudio: celular — não achei o relógio. Aparelhos de voz: " + (seen.length() == 0 ? "nenhum" : seen);
        return null;
    }

    private void releaseWatch(AudioManager am, boolean routed) {
        try {
            if (routed) {
                if (Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice();
                am.setBluetoothScoOn(false);
                am.stopBluetoothSco();
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
        if (method().startsWith("C") && callConversation()) return;
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        final boolean routed = routeToWatch(am) != null;
        boolean watch = routed;
        info(routeInfo);
        try {
            say(watch ? "Ouvindo pelo relógio..." : "Ouvindo pelo celular...");
            byte[] pcm = record(watch);
            ui.post(() -> mic.setText("🎙️"));
            if (pcm == null) {
                say("Não ouvi nada. Toque para falar.");
                return;
            }
            say("Pensando...");
            JSONObject res = http(prefs.getString("server", "") + "/api/v1/voice?audio=wav", "audio/wav", wav(pcm), prefs.getString("token", ""));
            if (res.optBoolean("ignored")) {
                say("Não entendi. Toque para tentar de novo.");
                return;
            }
            String heard = res.optString("heard", "");
            String reply = res.optString("reply", "");
            if (!heard.isEmpty()) addLog("Você", heard);
            if (!reply.isEmpty()) addLog(prefs.getString("agent", "Kronus"), reply);
            say("Respondendo...");
            playInfo = "";
            if (watch) watch = ensureWatch(am);
            if (watch) beeps(1);                         // o bipe acorda o canal antes da primeira frase
            speak(res, watch);
            if (!playInfo.isEmpty()) info(routeInfo + "\n🔊 " + playInfo);
            say("Toque para falar.");
        } catch (Exception e) {
            say("⚠️ " + e.getMessage());
            checkStatus();
        } finally {
            releaseWatch(am, routed || watch);
            listening = false;
            busy = false;
            ui.post(() -> mic.setText("🎙️"));
        }
    }

    /** v1.8: a conversa como CHAMADA. Turnos: você fala, ele responde, você pode continuar; silêncio encerra.
     *  Devolve false se a chamada nem começou (aí o app usa o modo antigo). */
    private boolean callConversation() {
        say("Ligando para o Kronus...");
        KronusCallService.KronusConnection k = KronusCallService.start(this, 6000);
        if (k == null) {
            info("📞 A chamada não começou: " + KronusCallService.lastError + " — usando o modo antigo.");
            return false;
        }
        try {
            boolean bt = k.waitBluetooth(4000);
            sleep(1200);                                     // o microfone do relógio leva um instante para estabilizar
            info(bt ? "📞 Em chamada com o Kronus pelo relógio" + (k.routeName.isEmpty() ? "" : " (" + k.routeName + ")")
                    : "📞 Em chamada, mas o Android deixou o áudio no celular");
            boolean follow = prefs.getBoolean("seguir", false);
            int misses = 0;
            for (int turn = 0; turn < (follow ? 4 : 1) && !k.ended; turn++) {
                listening = true;
                ui.post(() -> mic.setText("⏹️"));
                say(turn == 0 ? "Fale depois do bipe..." : "Pode continuar (ou fique em silêncio para desligar)...");
                beeps(1);                                    // "pode falar": evita perder as primeiras sílabas
                byte[] pcm = record(true, turn == 0 ? 8000 : 5000, turn > 0);
                listening = false;
                ui.post(() -> mic.setText("📞"));
                if (pcm == null || k.ended) break;
                say("Pensando...");
                JSONObject res = http(prefs.getString("server", "") + "/api/v1/voice?audio=wav", "audio/wav", wav(pcm),
                        prefs.getString("token", ""));
                if (res.optBoolean("ignored")) {
                    if (++misses >= 2 || turn > 0) break;      // ruído virando "fala": encerra em vez de insistir
                    playInCall(null, "Não entendi. Pode repetir?");
                    continue;
                }
                String heard = res.optString("heard", "");
                String reply = res.optString("reply", "");
                if (!heard.isEmpty()) addLog("Você", heard);
                if (!reply.isEmpty()) addLog(prefs.getString("agent", "Kronus"), reply);
                say("Respondendo...");
                byte[] wav = null;
                String b64 = res.optString("audio_b64", "");
                if (!b64.isEmpty() && !"null".equals(b64) && res.optString("audio_mime", "").contains("wav")) {
                    try { wav = Base64.decode(b64, Base64.DEFAULT); } catch (Exception ignored) { }
                }
                playInfo = "";
                playInCall(wav, res.optString("speech", reply));
                info((bt ? "📞 Em chamada pelo relógio" : "📞 Em chamada (áudio no celular)") + "\n🔊 " + playInfo);
            }
            say("Chamada encerrada. Toque para falar de novo.");
        } catch (Exception e) {
            say("⚠️ " + e.getMessage());
            checkStatus();
        } finally {
            k.hangUp();
            listening = false;
            busy = false;
            ui.post(() -> mic.setText("🎙️"));
        }
        return true;
    }

    /** Pede ao servidor a frase na voz do Kronus (WAV). null se não der. */
    private byte[] serverVoice(String text) {
        try {
            JSONObject body = new JSONObject();
            body.put("text", text);
            body.put("format", "wav");
            JSONObject res = http(prefs.getString("server", "") + "/api/v1/speak", "application/json",
                    body.toString().getBytes(StandardCharsets.UTF_8), prefs.getString("token", ""));
            String b64 = res.optString("audio_b64", "");
            return b64.isEmpty() || !res.optString("audio_mime", "").contains("wav") ? null : Base64.decode(b64, Base64.DEFAULT);
        } catch (Exception e) {
            return null;
        }
    }

    /** Fala dentro da chamada: C1 = voz do servidor tocada pelo app; C2 = voz do Android. Um cai no outro se falhar. */
    private void playInCall(byte[] wav, String text) {
        if ("C1".equals(method())) {
            Pcm pcm = wav != null ? Pcm.fromWav(wav) : null;
            if (pcm == null && ttsReady && text != null && !text.isEmpty()) {
                byte[] f = ttsToWav(text);
                pcm = f == null ? null : Pcm.fromWav(f);
            }
            if (pcm != null && playVoip(pcm)) return;
        }
        speakDirect(text, true);
    }

    /** Áudio de chamada (VoIP) pelo app: dentro de uma chamada ativa, o Android manda para o relógio. */
    private boolean playVoip(Pcm pcm) {
        Pcm p16 = pcm.resample(16000);
        byte[] data = p16.bytes(0.2, 0.4);
        int mask = AudioFormat.CHANNEL_OUT_MONO;
        int buf = Math.max(AudioTrack.getMinBufferSize(16000, mask, AudioFormat.ENCODING_PCM_16BIT) * 2, 16000);
        AudioTrack track;
        try {
            track = new AudioTrack.Builder()
                    .setAudioAttributes(new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                    .setAudioFormat(new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .setSampleRate(16000).setChannelMask(mask).build())
                    .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(buf).build();
        } catch (Exception e) {
            playInfo = "o Android recusou o áudio da chamada";
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
            long deadline = System.currentTimeMillis() + frames * 1000L / 16000 + 3000;
            while (track.getPlaybackHeadPosition() < frames && System.currentTimeMillis() < deadline) sleep(50);
            playInfo = String.format(Locale.US, "voz do Kronus: %.1f s", p16.s.length / 16000.0);
            return written > 0;
        } catch (Exception e) {
            return false;
        } finally {
            try { track.stop(); } catch (Exception ignored) { }
            track.release();
        }
    }

    /** Grava até a pessoa parar de falar (1,6 s de silêncio), no máximo 20 s. Devolve null se não houve fala. */
    private byte[] record(boolean watch) {
        return record(watch, 8000, false);
    }

    private byte[] record(boolean watch, long noSpeechMs) {
        return record(watch, noSpeechMs, false);
    }

    /** A última gravação enviada (para "Ouvir minha última gravação"). */
    private volatile byte[] lastRecording;

    /** strict=true (continuação da conversa): exige fala mais longa e mais alta que o ruído, para não pegar o ambiente. */
    private byte[] record(boolean watch, long noSpeechMs, boolean strict) {
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
        double floor = Double.MAX_VALUE;
        boolean heard = false;
        int loudBlocks = 0, speechStart = 0;
        int needBlocks = strict ? 7 : 4;                  // 350 ms (continuação) ou 200 ms de fala seguida
        double factor = strict ? 4.5 : 3.5, minLevel = strict ? 450 : (watch ? 260 : 350);
        if (watch) disableVoiceEffects(rec.getAudioSessionId());
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
                if (now - start < 400) {                    // início: o trecho MAIS silencioso é o ruído do ambiente
                    floor = Math.min(floor, rms);
                    continue;
                }
                double threshold = Math.max(minLevel, (floor == Double.MAX_VALUE ? 0 : floor) * factor);
                if (rms > threshold) {
                    loudBlocks++;
                    if (loudBlocks >= needBlocks && !heard) {   // fala sustentada acima do ruído
                        heard = true;
                        // a gravação enviada começa 0,3 s antes da fala (sem o ruído do começo)
                        speechStart = Math.max(0, out.size() - (needBlocks * n * 2) - (RATE * 2 * 3 / 10));
                        say("Ouvindo você... (pare de falar para enviar)");
                    }
                    lastVoice = now;
                } else {
                    loudBlocks = 0;
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
        int from = speechStart - (speechStart % 2);
        byte[] cut = java.util.Arrays.copyOfRange(all, from, all.length);
        byte[] result = normalize(cut);
        lastRecording = result;
        return result;
    }

    /** Os filtros de chamada (redutor de ruído, ganho automático, eco) "cortam" o começo da fala enquanto se ajustam;
     *  o app já ajusta o volume sozinho (normalize). Celulares sem esses filtros simplesmente ignoram. */
    static void disableVoiceEffects(int session) {
        try {
            if (android.media.audiofx.NoiseSuppressor.isAvailable()) {
                android.media.audiofx.NoiseSuppressor ns = android.media.audiofx.NoiseSuppressor.create(session);
                if (ns != null) ns.setEnabled(false);
            }
            if (android.media.audiofx.AutomaticGainControl.isAvailable()) {
                android.media.audiofx.AutomaticGainControl agc = android.media.audiofx.AutomaticGainControl.create(session);
                if (agc != null) agc.setEnabled(false);
            }
            if (android.media.audiofx.AcousticEchoCanceler.isAvailable()) {
                android.media.audiofx.AcousticEchoCanceler aec = android.media.audiofx.AcousticEchoCanceler.create(session);
                if (aec != null) aec.setEnabled(false);
            }
        } catch (Exception ignored) {
        }
    }

    /** O microfone do relógio (canal de ligação) costuma ser baixo: sobe o volume até ~80% sem distorcer. */
    static byte[] normalize(byte[] pcm) {
        ByteBuffer in = ByteBuffer.wrap(pcm).order(ByteOrder.LITTLE_ENDIAN);
        int peak = 1;
        for (int i = 0; i + 1 < pcm.length; i += 2) peak = Math.max(peak, Math.abs((int) in.getShort(i)));
        double gain = Math.min(8.0, (32767 * 0.8) / peak);
        if (gain <= 1.05) return pcm;
        ByteBuffer outBuf = ByteBuffer.allocate(pcm.length).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i + 1 < pcm.length; i += 2) {
            int v = (int) Math.round(in.getShort(i) * gain);
            outBuf.putShort((short) Math.max(-32768, Math.min(32767, v)));
        }
        return outBuf.array();
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

    // ------------------------------------------------------------------ saída do som (só canal de ligação)
    // v1.5: o relógio só aceita som no "canal de ligação" e, nele, só o bipe funcionou até aqui. Quatro métodos de tocar
    // fala nesse canal; o teste anuncia cada um com N bipes e a pessoa escolhe o que sai no relógio.
    //  A: AudioTrack clássico de ligação (o mesmo tipo do bipe) a 8 kHz, a taxa nativa do Bluetooth de voz
    //  B: "ligação completa": microfone aberto enquanto a resposta toca (como apps de chamada fazem)
    //  C: player de áudio do Android em modo ligação
    //  D: a voz do Android falando direto em modo ligação
    // v1.6: o teste mostrou que o canal fecha com silêncio digital (o bipe e a voz direta começam com som; tudo que
    // começava com silêncio sumiu, e a voz direta corta nas pausas). Ruído de fundo baixinho no lugar do silêncio:
    //  E: voz do servidor pelo app, com ruído no lugar dos silêncios (a melhor, se funcionar)
    //  F: voz do Android direta + ruído de fundo contínuo (o canal não fecha nas pausas)
    //  D: voz do Android direta (funciona, mas corta) — para comparar
    // v1.7: só a voz do Android falando direto (D) chegou ao relógio; som tocado pelo próprio app não chega, e o canal
    // abre e fecha sozinho (o celular reconhece o relógio como fone só por um instante). Como o INÍCIO de cada fala
    // sempre sai, a resposta é falada frase por frase, reabrindo o canal antes de cada uma quando ele fechou.
    //  D:  frase por frase, reabrindo o canal (padrão)
    //  D0: o texto inteiro de uma vez (como no 1.5)
    // v1.8: C1/C2 = a conversa vira uma CHAMADA do app; o Android mantém o relógio ligado do começo ao fim.
    //  C1: chamada + voz do Kronus (a do servidor)   C2: chamada + voz do Android
    static final String[] METHODS = {"C1", "C2", "D", "D0"};

    private volatile String playInfo = "";

    private String method() {
        String m = prefs.getString("metodo", "C1");
        return java.util.Arrays.asList(METHODS).contains(m) ? m : "C1";
    }

    /** Bipes que funcionam no relógio: anunciam qual método vem a seguir. */
    private void beeps(int n) {
        try {
            android.media.ToneGenerator tone = new android.media.ToneGenerator(AudioManager.STREAM_VOICE_CALL, 90);
            for (int i = 0; i < n; i++) {
                tone.startTone(android.media.ToneGenerator.TONE_PROP_BEEP, 180);
                sleep(380);
            }
            sleep(300);
            tone.release();
        } catch (Exception ignored) {
        }
    }

    /** Toca a resposta (voz do servidor; senão a voz do Android) pelo método escolhido. */
    private void speak(JSONObject res, boolean call) {
        String b64 = res.optString("audio_b64", "");
        String text = res.optString("speech", res.optString("reply", ""));
        String mime = res.optString("audio_mime", "");
        byte[] wav = null;
        if (!b64.isEmpty() && !"null".equals(b64) && mime.contains("wav")) {
            try {
                wav = Base64.decode(b64, Base64.DEFAULT);
            } catch (Exception ignored) {
            }
        }
        playSpeech(wav, text, call ? method() : "celular");
    }

    /** Toca a fala pelo método. No relógio: voz do Android (D frase por frase, D0 inteira). No celular: voz do servidor. */
    private boolean playSpeech(byte[] wav, String text, String m) {
        if ("D".equals(m)) return speakChunks(text);
        if ("D0".equals(m)) return speakDirect(text, true);
        Pcm pcm = wav == null ? null : Pcm.fromWav(wav);
        if (pcm != null && playTrack(pcm, false, false)) return true;
        return speakDirect(text, false);
    }

    /** Divide em trechos curtos pela pontuação (juntando pedaços muito pequenos). */
    static java.util.List<String> chunks(String text) {
        java.util.List<String> out = new java.util.ArrayList<>();
        StringBuilder cur = new StringBuilder();
        for (String part : text.trim().split("(?<=[.!?;:,…])\\s+")) {
            if (part.isEmpty()) continue;
            if (cur.length() > 0 && cur.length() + part.length() > 70) {
                out.add(cur.toString());
                cur.setLength(0);
            }
            if (cur.length() > 0) cur.append(' ');
            cur.append(part);
            if (cur.length() >= 22 && part.matches(".*[.!?;:,…]$")) {
                out.add(cur.toString());
                cur.setLength(0);
            }
        }
        if (cur.length() > 0) out.add(cur.toString());
        return out;
    }

    /** D: fala frase por frase; antes de cada uma, confere o canal do relógio e reabre se ele fechou. */
    private boolean speakChunks(String text) {
        if (!ttsReady || text == null || text.trim().isEmpty()) return false;
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        java.util.List<String> parts = chunks(text);
        int reopened = 0;
        boolean ok = true;
        for (String part : parts) {
            if (!scoActive(am)) {
                ensureWatch(am);
                reopened++;
            }
            ok &= speakDirect(part, true);
        }
        playInfo = parts.size() + " trecho(s)" + (reopened > 0 ? ", canal reaberto " + reopened + "x" : "");
        return ok;
    }

    /** Áudio PCM mono 16 bits em memória (de qualquer WAV PCM). */
    static final class Pcm {
        final short[] s;
        final int rate;

        Pcm(short[] s, int rate) { this.s = s; this.rate = rate; }

        static Pcm fromWav(byte[] wav) {
            ByteBuffer b = ByteBuffer.wrap(wav).order(ByteOrder.LITTLE_ENDIAN);
            if (wav.length < 44 || b.getInt(0) != 0x46464952 || b.getInt(8) != 0x45564157) return null;
            int rate = 0, channels = 1, bits = 0, dataAt = -1, dataLen = 0, pos = 12;
            while (pos + 8 <= wav.length) {
                int id = b.getInt(pos), len = b.getInt(pos + 4);
                if (id == 0x20746d66) {
                    int format = b.getShort(pos + 8) & 0xFFFF;
                    if (format != 1 && format != 0xFFFE) return null;
                    channels = Math.max(1, b.getShort(pos + 10));
                    rate = b.getInt(pos + 12);
                    bits = b.getShort(pos + 22);
                } else if (id == 0x61746164) {
                    dataAt = pos + 8;
                    int rest = wav.length - dataAt;
                    dataLen = (len <= 0 || len > rest) ? rest : len;  // tamanho zerado/inválido: vale o resto
                    break;
                }
                if (len < 0 || pos + 8 + len > wav.length) break;
                pos += 8 + len + (len & 1);
            }
            if (rate <= 0 || bits != 16 || dataAt < 0) return null;
            int frames = dataLen / (2 * channels);
            if (frames < rate / 5) return null;           // menos de 0,2 s
            short[] out = new short[frames];
            for (int f = 0; f < frames; f++) {
                int sum = 0;
                for (int c = 0; c < channels; c++) sum += b.getShort(dataAt + (f * channels + c) * 2);
                out[f] = (short) (sum / channels);
            }
            return new Pcm(out, rate);
        }

        Pcm resample(int to) {
            if (to == rate) return this;
            int n = (int) ((long) s.length * to / rate);
            short[] out = new short[n];
            double step = (double) rate / to;
            for (int i = 0; i < n; i++) {
                double x = i * step;
                int a = (int) x;
                int b2 = Math.min(a + 1, s.length - 1);
                double t = x - a;
                if (step > 1.5) {                          // reduzindo a taxa: média do trecho (evita chiado)
                    int end = Math.min(s.length, (int) ((i + 1) * step));
                    long sum = 0;
                    int cnt = 0;
                    for (int k = a; k < end; k++) { sum += s[k]; cnt++; }
                    out[i] = (short) (cnt > 0 ? sum / cnt : s[a]);
                } else {
                    out[i] = (short) (s[a] * (1 - t) + s[b2] * t);
                }
            }
            return new Pcm(out, to);
        }

        byte[] bytes(double leadSec, double tailSec) {
            return bytes(leadSec, tailSec, 0);
        }

        /** noise > 0: ruído baixinho no lugar do silêncio (abertura, fim e pausas da fala). */
        byte[] bytes(double leadSec, double tailSec, int noise) {
            int lead = (int) (rate * leadSec), tail = (int) (rate * tailSec);
            java.util.Random rnd = new java.util.Random(7);
            ByteBuffer bb = ByteBuffer.allocate((lead + s.length + tail) * 2).order(ByteOrder.LITTLE_ENDIAN);
            for (int i = 0; i < lead; i++) bb.putShort(noise > 0 ? (short) (rnd.nextInt(2 * noise + 1) - noise) : 0);
            for (short v : s) {
                int x = v + (noise > 0 ? rnd.nextInt(2 * noise + 1) - noise : 0);
                bb.putShort((short) Math.max(-32768, Math.min(32767, x)));
            }
            for (int i = 0; i < tail; i++) bb.putShort(noise > 0 ? (short) (rnd.nextInt(2 * noise + 1) - noise) : 0);
            return bb.array();
        }

        byte[] wav(double leadSec, double tailSec) {
            byte[] data = bytes(leadSec, tailSec);
            ByteBuffer h = ByteBuffer.allocate(44 + data.length).order(ByteOrder.LITTLE_ENDIAN);
            h.put("RIFF".getBytes(StandardCharsets.US_ASCII)).putInt(36 + data.length).put("WAVE".getBytes(StandardCharsets.US_ASCII));
            h.put("fmt ".getBytes(StandardCharsets.US_ASCII)).putInt(16).putShort((short) 1).putShort((short) 1)
                    .putInt(rate).putInt(rate * 2).putShort((short) 2).putShort((short) 16);
            h.put("data".getBytes(StandardCharsets.US_ASCII)).putInt(data.length);
            h.put(data);
            return h.array();
        }
    }

    /** AudioTrack. call=true: canal de ligação (clássico, como o bipe). keepMic=true: microfone aberto durante o som. */
    @SuppressWarnings("deprecation")
    private boolean playTrack(Pcm pcm, boolean call, boolean keepMic) {
        byte[] data = pcm.bytes(0.15, 0.5);
        int mask = AudioFormat.CHANNEL_OUT_MONO;
        int buf = Math.max(AudioTrack.getMinBufferSize(pcm.rate, mask, AudioFormat.ENCODING_PCM_16BIT) * 2, pcm.rate);
        AudioTrack track;
        try {
            if (call && !keepMic) {
                track = new AudioTrack(AudioManager.STREAM_VOICE_CALL, pcm.rate, mask, AudioFormat.ENCODING_PCM_16BIT, buf, AudioTrack.MODE_STREAM);
            } else {
                track = new AudioTrack.Builder()
                        .setAudioAttributes(new AudioAttributes.Builder()
                                .setUsage(call ? AudioAttributes.USAGE_VOICE_COMMUNICATION : AudioAttributes.USAGE_ASSISTANT)
                                .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build())
                        .setAudioFormat(new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                                .setSampleRate(pcm.rate).setChannelMask(mask).build())
                        .setTransferMode(AudioTrack.MODE_STREAM).setBufferSizeInBytes(buf).build();
            }
        } catch (Exception e) {
            playInfo = "o Android recusou o canal (" + e.getClass().getSimpleName() + ")";
            return false;
        }
        AudioRecord mic = null;
        final boolean[] micOn = {false};
        try {
            if (track.getState() != AudioTrack.STATE_INITIALIZED) {
                playInfo = "o Android não abriu o canal de áudio";
                return false;
            }
            if (keepMic) {                                 // "ligação completa": entrada e saída abertas juntas
                try {
                    mic = new AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, 16000, AudioFormat.CHANNEL_IN_MONO,
                            AudioFormat.ENCODING_PCM_16BIT, 16000);
                    mic.startRecording();
                    micOn[0] = true;
                    final AudioRecord m = mic;
                    new Thread(() -> {
                        short[] trash = new short[800];
                        while (micOn[0]) m.read(trash, 0, trash.length);
                    }).start();
                } catch (Exception ignored) {
                }
            }
            track.play();
            int written = 0;
            while (written < data.length) {
                int w = track.write(data, written, Math.min(4096, data.length - written));
                if (w <= 0) break;
                written += w;
            }
            long frames = written / 2;
            long deadline = System.currentTimeMillis() + frames * 1000L / pcm.rate + 3000;
            while (track.getPlaybackHeadPosition() < frames && System.currentTimeMillis() < deadline) sleep(50);
            playInfo = String.format(Locale.US, "tocou %.1f s a %d Hz", pcm.s.length / (double) pcm.rate, pcm.rate);
            return written > 0;
        } catch (Exception e) {
            playInfo = "erro ao tocar: " + e.getClass().getSimpleName();
            return false;
        } finally {
            micOn[0] = false;
            if (mic != null) {
                try { mic.stop(); } catch (Exception ignored) { }
                mic.release();
            }
            try { track.stop(); } catch (Exception ignored) { }
            track.release();
        }
    }

    /** A voz do Android falando direto (em modo ligação quando call=true). Espera terminar. */
    private boolean speakDirect(String text, boolean call) {
        if (!ttsReady || text == null || text.isEmpty()) return false;
        tts.setLanguage(new Locale("pt", "BR"));
        CountDownLatch done = new CountDownLatch(1);
        final boolean[] ok = {true};
        tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
            @Override public void onStart(String id) { }
            @Override public void onDone(String id) { done.countDown(); }
            @Override public void onError(String id) { ok[0] = false; done.countDown(); }
        });
        Bundle params = new Bundle();
        params.putInt(TextToSpeech.Engine.KEY_PARAM_STREAM, call ? AudioManager.STREAM_VOICE_CALL : AudioManager.STREAM_MUSIC);
        tts.speak(text, TextToSpeech.QUEUE_FLUSH, params, "direta");
        try {
            done.await(60, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
        }
        if (playInfo.isEmpty()) playInfo = "voz do Android falou direto";
        return ok[0];
    }

    /** A voz do Android grava num arquivo WAV (em vez de tocar sozinha, onde quiser). */
    private byte[] ttsToWav(String text) {
        try {
            tts.setLanguage(new Locale("pt", "BR"));
            File f = new File(getCacheDir(), "fala.wav");
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

    // ------------------------------------------------------------------ rede
    private static JSONObject http(String url, String ctype, byte[] body, String token) throws Exception {
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
