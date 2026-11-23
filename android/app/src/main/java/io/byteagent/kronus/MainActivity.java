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
    private static final int REQ_LOCATION = 8;
    static final String ACTION_ANSWER = "io.byteagent.kronus.ATENDER";
    private static final Pattern LINK = Pattern.compile("(https://[A-Za-z0-9.\\-]+(?::\\d+)?)/voz#parear=(\\d{6})");
    private static final Pattern CODE = Pattern.compile("^\\s*(\\d{6})\\s*$");

    private SharedPreferences prefs;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private TextView title, conn, state, diag;
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
        Conversa.listener = null;
        ReuniaoService.listener = null;
        ui.removeCallbacks(pollNotes);
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
        Conversa.listener = () -> ui.post(this::renderChat);
        ReuniaoService.listener = (st, txt) -> ui.post(this::refreshMeeting);
        refreshMeeting();
        renderChat();
        ui.removeCallbacks(pollNotes);
        ui.post(pollNotes);
        checkStatus();
        maybeAutoListen();
        if (paired() && Local.allowed(this)) new Thread(() -> Local.sendIfDue(this, false), "kronus-local").start();
    }

    /** v1.13: manda a posição agora e mostra onde o Kronus acha que você está. */
    private void shareLocationNow() {
        say("📍 Localizando...");
        new Thread(() -> {
            String where = Local.sendIfDue(this, true);
            ui.post(() -> say(where.isEmpty() ? "📍 Localização ligada: o Kronus vai usar onde você está." : where));
            if (KronusWatchService.isRunning()) {           // o serviço passa a poder ler a posição em segundo plano
                try { KronusWatchService.start(this); } catch (Exception ignored) { }
            }
        }, "kronus-local").start();
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

    // ------------------------------------------------------------------ app 2.0: abas, conversa e comandos
    private static final int BG = Color.rgb(11, 15, 23), CARD = Color.rgb(24, 31, 45), INK = Color.rgb(238, 242, 247),
            MUTED = Color.rgb(151, 161, 178), BLUE = Color.rgb(37, 99, 235), AMBER = Color.rgb(245, 158, 11);
    private static final String[] TABS = {"💬 Conversa", "⚡ Comandos", "⌚ Relógio", "⚙️ Ajustes"};
    private final View[] pages = new View[4];
    private final Button[] tabButtons = new Button[4];
    private int currentTab = 0;
    private LinearLayout chatList, cmdList;
    private ScrollView chatScroll;
    private EditText msgInput;
    private TextView settingsInfo;

    private GradientDrawable rounded(int color, int radiusDp, int strokeColor) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(color);
        g.setCornerRadius(dp(radiusDp));
        if (strokeColor != 0) g.setStroke(dp(1), strokeColor);
        return g;
    }

    private TextView label(String text, int sizeSp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(text);
        t.setTextSize(sizeSp);
        t.setTextColor(color);
        if (bold) t.setTypeface(Typeface.DEFAULT_BOLD);
        return t;
    }

    private Button pill(String text, View.OnClickListener click) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextColor(INK);
        b.setTextSize(14);
        b.setBackground(rounded(CARD, 18, Color.rgb(51, 65, 85)));
        b.setPadding(dp(14), dp(6), dp(14), dp(6));
        b.setOnClickListener(click);
        return b;
    }

    private CheckBox check(String text, String pref, boolean def, int color) {
        CheckBox cb = new CheckBox(this);
        cb.setText(text);
        cb.setTextColor(color);
        cb.setTextSize(15);
        cb.setPadding(0, dp(6), 0, dp(6));
        cb.setChecked(prefs.getBoolean(pref, def));
        cb.setOnCheckedChangeListener((b, on) -> prefs.edit().putBoolean(pref, on).apply());
        return cb;
    }

    private LinearLayout section(LinearLayout parent, String title, String hint) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setBackground(rounded(CARD, 14, 0));
        box.setPadding(dp(14), dp(12), dp(14), dp(12));
        box.addView(label(title, 16, INK, true));
        if (hint != null && !hint.isEmpty()) {
            TextView h = label(hint, 13, MUTED, false);
            h.setPadding(0, dp(4), 0, dp(4));
            box.addView(h);
        }
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.bottomMargin = dp(12);
        parent.addView(box, lp);
        return box;
    }

    /** Uma aba com rolagem: tudo cabe, em qualquer tamanho de tela. */
    private LinearLayout scrollPage(int index) {
        ScrollView sv = new ScrollView(this);
        sv.setFillViewport(true);
        LinearLayout inner = new LinearLayout(this);
        inner.setOrientation(LinearLayout.VERTICAL);
        inner.setPadding(0, dp(8), 0, dp(24));
        sv.addView(inner);
        pages[index] = sv;
        return inner;
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(BG);
        root.setOnApplyWindowInsetsListener((v, insets) -> {
            int top, bottom;
            if (Build.VERSION.SDK_INT >= 30) {
                android.graphics.Insets s = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
                top = s.top; bottom = s.bottom;
            } else {
                top = insets.getSystemWindowInsetTop(); bottom = insets.getSystemWindowInsetBottom();
            }
            v.setPadding(dp(12), top + dp(8), dp(12), bottom + dp(4));
            return insets;
        });

        // cabeçalho: nome e estado da conexão (toque para conferir de novo)
        title = label("Kronus", 22, INK, true);
        root.addView(title);
        conn = label("", 12, MUTED, false);
        conn.setOnClickListener(v -> checkStatus());
        root.addView(conn);

        android.widget.FrameLayout content = new android.widget.FrameLayout(this);
        root.addView(content, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));
        buildChatPage();
        buildCommandsPage();
        buildWatchPage();
        buildSettingsPage();
        for (View p : pages) content.addView(p, new android.widget.FrameLayout.LayoutParams(-1, -1));

        // barra de abas embaixo
        LinearLayout nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setPadding(0, dp(6), 0, 0);
        for (int i = 0; i < TABS.length; i++) {
            final int idx = i;
            Button b = new Button(this);
            b.setText(TABS[i].replace(" ", "\n"));
            b.setAllCaps(false);
            b.setTextSize(12);
            b.setBackgroundColor(Color.TRANSPARENT);
            b.setOnClickListener(v -> showTab(idx));
            tabButtons[i] = b;
            nav.addView(b, new LinearLayout.LayoutParams(0, dp(58), 1f));
        }
        root.addView(nav);
        setContentView(root);
        showTab(0);
    }

    private void showTab(int idx) {
        currentTab = idx;
        for (int i = 0; i < pages.length; i++) {
            pages[i].setVisibility(i == idx ? View.VISIBLE : View.GONE);
            tabButtons[i].setTextColor(i == idx ? INK : MUTED);
            tabButtons[i].setTypeface(i == idx ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
        }
        if (idx == 0) scrollChatToEnd();
        if (idx == 1 && paired()) loadCommands(false);
    }

    // ---------------- aba Conversa
    private void buildChatPage() {
        LinearLayout page = new LinearLayout(this);
        page.setOrientation(LinearLayout.VERTICAL);
        pages[0] = page;

        chatScroll = new ScrollView(this);
        chatList = new LinearLayout(this);
        chatList.setOrientation(LinearLayout.VERTICAL);
        chatList.setPadding(0, dp(8), 0, dp(8));
        chatScroll.addView(chatList);
        page.addView(chatScroll, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f));

        state = label("", 13, MUTED, false);
        state.setGravity(Gravity.CENTER_HORIZONTAL);
        state.setPadding(0, dp(4), 0, dp(4));
        page.addView(state);

        // atalhos rápidos (os mais usados); o resto fica na aba Comandos
        android.widget.HorizontalScrollView hs = new android.widget.HorizontalScrollView(this);
        hs.setHorizontalScrollBarEnabled(false);
        LinearLayout chips = new LinearLayout(this);
        chips.setOrientation(LinearLayout.HORIZONTAL);
        String[][] quick = {{"☀️ Resumo", "/resumo"}, {"🌤️ Clima", "/clima"}, {"🗓️ Agenda", "/agenda"},
                {"⏰ Lembretes", "/lembretes"}, {"🧭 Rotina", "/rotina"}, {"⚡ Mais", ""}};
        for (String[] q : quick) {
            Button b = pill(q[0], v -> {
                if (q[1].isEmpty()) showTab(1);
                else sendText(q[1]);
            });
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-2, dp(40));
            lp.rightMargin = dp(6);
            chips.addView(b, lp);
        }
        hs.addView(chips);
        page.addView(hs);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(6), 0, 0);
        msgInput = new EditText(this);
        msgInput.setHint("Escreva para o Kronus...");
        msgInput.setTextColor(INK);
        msgInput.setHintTextColor(MUTED);
        msgInput.setMaxLines(4);
        msgInput.setBackground(rounded(CARD, 22, Color.rgb(51, 65, 85)));
        msgInput.setPadding(dp(14), dp(10), dp(14), dp(10));
        row.addView(msgInput, new LinearLayout.LayoutParams(0, -2, 1f));
        Button send = new Button(this);
        send.setText("➤");
        send.setTextColor(Color.WHITE);
        send.setTextSize(20);
        send.setBackground(rounded(BLUE, 24, 0));
        send.setOnClickListener(v -> {
            String t = msgInput.getText().toString().trim();
            if (!t.isEmpty()) {
                msgInput.setText("");
                sendText(t);
            }
        });
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(dp(48), dp(48));
        sp.leftMargin = dp(6);
        row.addView(send, sp);
        mic = new Button(this);
        mic.setText("🎙️");
        mic.setTextSize(20);
        GradientDrawable circle = new GradientDrawable();
        circle.setShape(GradientDrawable.OVAL);
        circle.setColor(Color.rgb(15, 118, 110));
        mic.setBackground(circle);
        mic.setOnClickListener(v -> {
            if (listening) listening = false;          // tocar enquanto ouve: termina de ouvir e envia
            else if (KronusCallService.current != null) {
                KronusCallService.end();               // tocar durante a chamada (fora da escuta): desliga
                say("Chamada encerrada.");
            } else startListening();
        });
        LinearLayout.LayoutParams mp = new LinearLayout.LayoutParams(dp(48), dp(48));
        mp.leftMargin = dp(6);
        row.addView(mic, mp);
        page.addView(row);
        renderChat();
    }

    private void scrollChatToEnd() {
        if (chatScroll != null) chatScroll.post(() -> chatScroll.fullScroll(View.FOCUS_DOWN));
    }

    /** Desenha as mensagens: as suas à direita, as do Kronus à esquerda, os avisos no meio. */
    private void renderChat() {
        if (chatList == null) return;
        chatList.removeAllViews();
        java.util.List<Conversa.Msg> all = Conversa.all(this);
        if (all.isEmpty()) {
            TextView empty = label(paired() ? "Escreva, toque em 🎙️ para falar ou use os atalhos abaixo."
                    : "Conecte o app ao Kronus na aba ⚙️ Ajustes.", 14, MUTED, false);
            empty.setGravity(Gravity.CENTER);
            empty.setPadding(dp(16), dp(40), dp(16), dp(16));
            chatList.addView(empty);
            return;
        }
        int from = Math.max(0, all.size() - 120);
        for (int i = from; i < all.size(); i++) {
            Conversa.Msg m = all.get(i);
            boolean me = Conversa.EU.equals(m.who), alert = Conversa.AVISO.equals(m.who);
            LinearLayout wrap = new LinearLayout(this);
            wrap.setOrientation(LinearLayout.VERTICAL);
            wrap.setGravity(me ? Gravity.END : (alert ? Gravity.CENTER_HORIZONTAL : Gravity.START));
            TextView bubble = label(m.text, 15, INK, false);
            bubble.setTextIsSelectable(true);
            bubble.setPadding(dp(12), dp(8), dp(12), dp(8));
            bubble.setBackground(rounded(me ? BLUE : CARD, 16, alert ? AMBER : 0));
            bubble.setMaxWidth((int) (getResources().getDisplayMetrics().widthPixels * 0.82));
            wrap.addView(bubble);
            if (m.buttons != null && m.buttons.length() > 0 && !m.used) {
                LinearLayout btns = new LinearLayout(this);
                btns.setOrientation(LinearLayout.HORIZONTAL);
                for (int j = 0; j < m.buttons.length(); j++) {
                    JSONObject b = m.buttons.optJSONObject(j);
                    if (b == null) continue;
                    final String data = b.optString("data");
                    Button btn = pill(b.optString("label"), v -> {
                        Conversa.markUsed(this, m);
                        sendCallback(data);
                    });
                    LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-2, dp(42));
                    lp.rightMargin = dp(6);
                    lp.topMargin = dp(4);
                    btns.addView(btn, lp);
                }
                wrap.addView(btns);
            }
            LinearLayout.LayoutParams wp = new LinearLayout.LayoutParams(-1, -2);
            wp.bottomMargin = dp(8);
            chatList.addView(wrap, wp);
        }
        scrollChatToEnd();
    }

    /** Mensagem escrita ou comando ("/clima"): vai pelo mesmo caminho do Telegram e volta com os botões. */
    private void sendText(String text) {
        if (!paired()) {
            showTab(3);
            say("Conecte o app primeiro.");
            return;
        }
        Conversa.add(this, Conversa.EU, text, null);
        showTab(0);
        say(text.startsWith("/") ? "Executando..." : "Kronus está pensando...");
        new Thread(() -> postCommand("text", text), "kronus-msg").start();
    }

    private void sendCallback(String data) {
        say("Enviando...");
        new Thread(() -> postCommand("callback", data), "kronus-botao").start();
    }

    private void postCommand(String key, String value) {
        try {
            JSONObject body = new JSONObject();
            body.put(key, value);
            JSONObject res = http(prefs.getString("server", "") + "/api/v1/command", "application/json",
                    body.toString().getBytes(StandardCharsets.UTF_8), prefs.getString("token", ""));
            org.json.JSONArray msgs = res.optJSONArray("messages");
            StringBuilder spoken = new StringBuilder();
            for (int i = 0; msgs != null && i < msgs.length(); i++) {
                JSONObject m = msgs.getJSONObject(i);
                Conversa.add(this, Conversa.KRONUS, m.optString("text"), m.optJSONArray("buttons"));
                spoken.append(m.optString("text")).append(". ");
            }
            if (msgs == null || msgs.length() == 0) Conversa.add(this, Conversa.AVISO, "(sem resposta)", null);
            say("");
            if (prefs.getBoolean("ler_respostas", false) && ttsReady && spoken.length() > 0)
                tts.speak(spoken.toString().replaceAll("[\\p{So}\\p{Cn}*_#>`]", " "), TextToSpeech.QUEUE_FLUSH, null, "resposta");
        } catch (Exception e) {
            Conversa.add(this, Conversa.AVISO, "⚠️ " + e.getMessage(), null);
            say("");
        }
    }

    // ---------------- aba Comandos (os comandos do Telegram como botões)
    private void buildCommandsPage() {
        LinearLayout page = scrollPage(1);
        buildMeetingSection(page);
        cmdList = new LinearLayout(this);
        cmdList.setOrientation(LinearLayout.VERTICAL);
        page.addView(cmdList);
        renderCommands(prefs.getString("comandos", ""));
    }

    // ---------------- reunião (app 2.1): grava, transcreve ao vivo, ata no fim
    private static final int REQ_FILE = 21;
    private TextView meetingState, meetingLive;
    private Button meetingBtn;
    private EditText meetingTitle;

    private void buildMeetingSection(LinearLayout page) {
        LinearLayout box = section(page, "📝 Reunião", "Grave a reunião: o texto aparece aqui enquanto ela acontece e, no fim, "
                + "a ata (resumo, decisões e tarefas) chega na conversa e no Telegram.");
        meetingTitle = new EditText(this);
        meetingTitle.setHint("Título (opcional), ex.: Reunião de orçamento");
        meetingTitle.setTextColor(INK);
        meetingTitle.setHintTextColor(MUTED);
        meetingTitle.setSingleLine(true);
        box.addView(meetingTitle);
        meetingBtn = pill("🔴 Iniciar gravação", v -> toggleMeeting());
        box.addView(meetingBtn);
        meetingState = label("", 13, MUTED, false);
        box.addView(meetingState);
        meetingLive = label("", 14, INK, false);
        meetingLive.setTextIsSelectable(true);
        meetingLive.setMaxLines(14);
        meetingLive.setVerticalScrollBarEnabled(true);
        meetingLive.setMovementMethod(new android.text.method.ScrollingMovementMethod());
        box.addView(meetingLive);
        box.addView(pill("📤 Enviar uma gravação (áudio ou vídeo, até 150 MB)", v -> {
            Intent pick = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
                    .putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"audio/*", "video/*"});
            startActivityForResult(pick, REQ_FILE);
        }));
        refreshMeeting();
    }

    private void toggleMeeting() {
        if (!paired()) {
            showTab(3);
            return;
        }
        if (ReuniaoService.recording) {
            ReuniaoService.stop(this);
            meetingBtn.setText("⏳ Encerrando...");
            return;
        }
        if (!hasMic()) {
            askPermissions();
            return;
        }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_PERMS);
        ReuniaoService.start(this, meetingTitle.getText().toString().trim());
        meetingTitle.setText("");
        meetingBtn.setText("⏹️ Encerrar e gerar ata");
        meetingState.setText("Começando...");
        meetingLive.setText("");
    }

    private void refreshMeeting() {
        if (meetingBtn == null) return;
        meetingBtn.setText(ReuniaoService.recording ? "⏹️ Encerrar e gerar ata" : "🔴 Iniciar gravação");
        meetingState.setText(ReuniaoService.lastState);
        synchronized (ReuniaoService.live) {
            meetingLive.setText(ReuniaoService.live.toString());
        }
    }

    @Override
    protected void onActivityResult(int code, int result, Intent data) {
        super.onActivityResult(code, result, data);
        if (code != REQ_FILE || result != RESULT_OK || data == null || data.getData() == null) return;
        android.net.Uri uri = data.getData();
        String title = meetingTitle.getText().toString().trim();
        meetingState.setText("📤 Enviando a gravação...");
        new Thread(() -> {
            String msg = ReuniaoService.uploadFile(this, uri, title);
            ui.post(() -> meetingState.setText(msg));
            Conversa.add(this, Conversa.AVISO, msg, null);
        }, "kronus-envio-gravacao").start();
    }

    private void loadCommands(boolean force) {
        long last = prefs.getLong("comandos_at", 0);
        if (!force && System.currentTimeMillis() - last < 10 * 60 * 1000L && !prefs.getString("comandos", "").isEmpty()) return;
        new Thread(() -> {
            try {
                JSONObject res = http(prefs.getString("server", "") + "/api/v1/commands", null, null, prefs.getString("token", ""));
                String json = String.valueOf(res.optJSONArray("commands"));
                prefs.edit().putString("comandos", json).putLong("comandos_at", System.currentTimeMillis()).apply();
                ui.post(() -> renderCommands(json));
            } catch (Exception ignored) {
                // sem rede agora: fica a lista guardada
            }
        }, "kronus-comandos").start();
    }

    private void renderCommands(String json) {
        cmdList.removeAllViews();
        org.json.JSONArray arr;
        try {
            arr = new org.json.JSONArray(json == null || json.isEmpty() || "null".equals(json) ? "[]" : json);
        } catch (Exception e) {
            arr = new org.json.JSONArray();
        }
        if (arr.length() == 0) {
            cmdList.addView(label(paired() ? "Carregando os comandos..." : "Conecte o app na aba ⚙️ Ajustes para ver os comandos.",
                    14, MUTED, false));
            return;
        }
        String group = null;
        LinearLayout box = null, row = null;
        for (int i = 0; i < arr.length(); i++) {
            JSONObject c = arr.optJSONObject(i);
            if (c == null) continue;
            if (!c.optString("group").equals(group)) {
                group = c.optString("group");
                box = section(cmdList, group, null);
                row = null;
            }
            if (row == null || row.getChildCount() == 2) {
                row = new LinearLayout(this);
                row.setOrientation(LinearLayout.HORIZONTAL);
                box.addView(row);
            }
            Button b = pill(c.optString("label"), v -> runCommand(c));
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, dp(52), 1f);
            lp.setMargins(dp(3), dp(4), dp(3), dp(4));
            row.addView(b, lp);
        }
        if (row != null && row.getChildCount() == 1) row.addView(new View(this), new LinearLayout.LayoutParams(0, 1, 1f));
        Button refreshBtn = pill("↻ Atualizar a lista", v -> loadCommands(true));
        cmdList.addView(refreshBtn);
    }

    /** Comando sem texto: roda na hora. Com texto: pergunta numa caixinha (com um exemplo). */
    private void runCommand(JSONObject c) {
        String cmd = c.optString("command"), ask = c.optString("ask");
        if (ask.isEmpty()) {
            sendText(cmd);
            return;
        }
        EditText field = new EditText(this);
        field.setHint(c.optString("example"));
        new android.app.AlertDialog.Builder(this)
                .setTitle(c.optString("label"))
                .setMessage(ask)
                .setView(field)
                .setPositiveButton("Enviar", (d, w) -> {
                    String arg = field.getText().toString().trim();
                    if (arg.isEmpty() && c.optBoolean("required")) {
                        say("Esse comando precisa de um texto.");
                        return;
                    }
                    sendText(arg.isEmpty() ? cmd : cmd + " " + arg);
                })
                .setNegativeButton("Cancelar", null)
                .show();
    }

    // ---------------- aba Relógio
    private void buildWatchPage() {
        LinearLayout page = scrollPage(2);

        LinearLayout watch = section(page, "⌚ Relógio", "Conversa por voz pelo microfone e alto-falante do relógio Bluetooth.");
        useWatch = check("Usar microfone e alto-falante do relógio", "watch", true, INK);
        useWatch.setOnCheckedChangeListener((b, on) -> prefs.edit().putBoolean("watch", on).apply());
        watch.addView(useWatch);
        watch.addView(check("Continuar a conversa depois da resposta (escuta de novo)", "seguir", false, MUTED));
        watch.addView(label("Método de som (use o que o teste mostrar que funciona):", 13, MUTED, false));
        android.widget.RadioGroup methods = new android.widget.RadioGroup(this);
        methods.setOrientation(LinearLayout.HORIZONTAL);
        for (int i = 0; i < METHODS.length; i++) {
            android.widget.RadioButton rb = new android.widget.RadioButton(this);
            rb.setId(1000 + i);
            rb.setText(METHODS[i]);
            rb.setTextColor(INK);
            methods.addView(rb);
            if (METHODS[i].equals(method())) rb.setChecked(true);
        }
        methods.setOnCheckedChangeListener((g, id) -> {
            prefs.edit().putString("metodo", METHODS[id - 1000]).apply();
            checkStatus();
        });
        watch.addView(methods);
        watch.addView(pill("🔊 Testar som do relógio", v -> testWatch()));
        watch.addView(pill("▶️ Ouvir minha última gravação", v -> replayLast()));
        diag = label("", 13, MUTED, false);
        diag.setPadding(0, dp(6), 0, 0);
        watch.addView(diag);

        LinearLayout calls = section(page, "📞 Kronus te liga", "Avisos tocam como chamada recebida no relógio.");
        CheckBox callsOn = check("Kronus pode me ligar para avisar", "ligar", false, INK);
        calls.addView(callsOn);
        LinearLayout callOpts = new LinearLayout(this);
        callOpts.setOrientation(LinearLayout.VERTICAL);
        callOpts.setPadding(dp(20), 0, 0, 0);
        String[][] kinds = {{"ligar_lembretes", "Lembretes", "1"}, {"ligar_agenda", "Compromissos chegando e hora de sair (agenda)", "1"},
                {"ligar_resumo", "Resumo da manhã e fechamento da semana (lidos em voz)", "0"},
                {"ligar_outros", "Outros avisos (objetivos, tarefas paradas...)", "0"}};
        for (String[] kind : kinds) callOpts.addView(check(kind[1], kind[0], "1".equals(kind[2]), MUTED));
        callOpts.setVisibility(callsOn.isChecked() ? View.VISIBLE : View.GONE);
        callsOn.setOnCheckedChangeListener((b, on) -> {
            prefs.edit().putBoolean("ligar", on).apply();
            callOpts.setVisibility(on ? View.VISIBLE : View.GONE);
            if (on) {
                if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_PERMS);
                KronusWatchService.start(this);
                say("Kronus atento: lembretes vão tocar como chamada.");
            } else {
                KronusWatchService.stop(this);
                say("O Kronus não vai mais ligar; os avisos chegam na conversa e no Telegram.");
            }
        });
        calls.addView(callOpts);
        calls.addView(pill("📲 Testar: Kronus me liga (em 5 s)", v -> {
            say("Em 5 segundos o Kronus vai ligar. Pode bloquear o celular.");
            ui.postDelayed(() -> {
                if (!KronusCallService.ring(this, "Isto é um teste do Kronus te liga. Se você atendeu pelo relógio, deu certo. "
                        + "Pode me responder depois do bipe."))
                    say("⚠️ Não consegui ligar: " + KronusCallService.lastError);
            }, 5000);
        }));
    }

    // ---------------- aba Ajustes
    private void buildSettingsPage() {
        LinearLayout page = scrollPage(3);

        setup = section(page, "🔗 Conectar ao Kronus", "Mande /parear no Telegram do Kronus, segure o link que ele enviar, "
                + "toque em Copiar e depois em \"Colar do Telegram\".");
        input = new EditText(this);
        input.setHint("link do /parear (ou código, se já conectou antes)");
        input.setTextColor(INK);
        input.setHintTextColor(MUTED);
        input.setSingleLine(true);
        setup.addView(input);
        setup.addView(pill("📋 Colar do Telegram", v -> pasteFromClipboard()));
        setup.addView(pill("Conectar", v -> pair(input.getText().toString())));

        LinearLayout where = section(page, "📍 Localização", "Clima, resumo e sugestões da rotina seguem onde você está "
                + "(posição aproximada, ~1 km; o servidor guarda só a última).");
        CheckBox loc = check("Compartilhar minha localização aproximada", Local.PREF, false, INK);
        loc.setOnCheckedChangeListener((b, on) -> {
            prefs.edit().putBoolean(Local.PREF, on).apply();
            if (!on) {
                say("Localização desligada: o Kronus usa a cidade do resumo.");
            } else if (checkSelfPermission(Manifest.permission.ACCESS_COARSE_LOCATION) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.ACCESS_COARSE_LOCATION}, REQ_LOCATION);
            } else {
                shareLocationNow();
            }
        });
        where.addView(loc);

        LinearLayout talk = section(page, "💬 Conversa", null);
        talk.addView(check("Ler em voz alta as respostas escritas", "ler_respostas", false, INK));
        talk.addView(pill("🧹 Apagar o histórico deste celular", v -> new android.app.AlertDialog.Builder(this)
                .setMessage("Apagar as mensagens guardadas neste celular? (O Kronus continua lembrando do que importa.)")
                .setPositiveButton("Apagar", (d, w) -> Conversa.clear(this))
                .setNegativeButton("Cancelar", null).show()));

        LinearLayout device = section(page, "📱 Este aparelho", null);
        settingsInfo = label("", 13, MUTED, false);
        device.addView(settingsInfo);
        disconnect = pill("Desconectar este aparelho", v -> {
            prefs.edit().remove("token").apply();
            say("Desconectado. Para remover o acesso de vez, mande /desparear no Telegram.");
            refresh();
        });
        device.addView(disconnect);
    }

    private boolean paired() {
        return !prefs.getString("token", "").isEmpty() && !prefs.getString("server", "").isEmpty();
    }

    private void refresh() {
        boolean ok = paired();
        setup.setVisibility(ok ? View.GONE : View.VISIBLE);
        disconnect.setVisibility(ok ? View.VISIBLE : View.GONE);
        title.setText(prefs.getString("agent", "Kronus"));
        String version = "?";
        try {
            version = getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception ignored) {
        }
        settingsInfo.setText((ok ? "Conectado a " + prefs.getString("server", "") : "Não conectado") + "\nApp " + version);
        renderChat();
        renderCommands(prefs.getString("comandos", ""));
        if (ok) {
            say("");
            loadCommands(false);
        } else {
            showTab(3);
        }
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

    /** Conversa por voz também entra no histórico (app 2.0). */
    private void addLog(String who, String text) {
        Conversa.add(this, "Você".equals(who) ? Conversa.EU : Conversa.KRONUS, text, null);
    }

    /** Sem o serviço atento ligado, a tela aberta busca os avisos (lembretes, resumos, rotina) a cada 20 s. */
    private final Runnable pollNotes = new Runnable() {
        @Override
        public void run() {
            if (paired() && !KronusWatchService.isRunning()) new Thread(MainActivity.this::fetchNotes, "kronus-avisos-tela").start();
            ui.postDelayed(this, 20000);
        }
    };

    private void fetchNotes() {
        try {
            JSONObject res = http(prefs.getString("server", "") + "/api/v1/notifications?canal=app", null, null, prefs.getString("token", ""));
            org.json.JSONArray items = res.optJSONArray("notifications");
            for (int i = 0; items != null && i < items.length(); i++)
                Conversa.add(this, Conversa.AVISO, items.getJSONObject(i).optString("text"), items.getJSONObject(i).optJSONArray("buttons"));
        } catch (Exception ignored) {
        }
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
        if (code == REQ_LOCATION) {
            if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) shareLocationNow();
            else say("Sem permissão de localização: o Kronus usa a cidade do resumo.");
            return;
        }
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
        con.setRequestProperty("User-Agent", "kronus-android/2.1");
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
