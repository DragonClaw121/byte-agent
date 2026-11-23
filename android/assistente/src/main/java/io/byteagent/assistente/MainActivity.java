package io.byteagent.assistente;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.ViewGroup;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

/**
 * "Meu Assistente": o app de quem foi convidado para um agente. Na primeira vez, a pessoa cola o link do painel
 * pessoal (o agente manda com /painel); depois o app abre direto no painel (conversa, comandos e e-mail) e os avisos
 * chegam como notificação.
 */
public class MainActivity extends Activity {
    private WebView web;
    private TextView status;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        askNotifications();
        String shared = sharedText(getIntent());
        if (shared != null && Rede.parse(shared) != null) {
            showSetup(shared);                                  // um link novo compartilhado com o app
        } else if (Rede.connected(this)) {
            showPanel();
        } else {
            showSetup(null);
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        String shared = sharedText(intent);
        if (shared != null && Rede.parse(shared) != null) showSetup(shared);
    }

    private static String sharedText(Intent intent) {
        if (intent == null || !Intent.ACTION_SEND.equals(intent.getAction())) return null;
        return intent.getStringExtra(Intent.EXTRA_TEXT);
    }

    private void askNotifications() {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density);
    }

    // ------------------------------------------------------------------ primeira vez: colar o link
    private void showSetup(String prefill) {
        web = null;
        kronusButton = null;
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setPadding(dp(24), dp(48), dp(24), dp(24));
        col.setBackgroundColor(Color.rgb(18, 21, 28));

        TextView title = new TextView(this);
        title.setText("Meu Assistente");
        title.setTextSize(26);
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setTextColor(Color.WHITE);
        col.addView(title);

        TextView info = new TextView(this);
        info.setText("Cole aqui o link do seu painel pessoal. Ele começa com https:// e tem um # no meio. "
                + "Se você ainda não tem, peça no Telegram com /painel ou para quem te convidou.\n\n"
                + "Dica: no Telegram, toque e segure o link → Copiar, ou use Compartilhar → Meu Assistente.");
        info.setTextColor(Color.rgb(170, 178, 196));
        info.setTextSize(15);
        info.setPadding(0, dp(12), 0, dp(16));
        col.addView(info);

        EditText input = new EditText(this);
        input.setHint("https://.../u/...#...");
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        input.setTextColor(Color.WHITE);
        input.setHintTextColor(Color.rgb(110, 118, 136));
        input.setSingleLine(true);
        if (prefill != null) input.setText(prefill.trim());
        col.addView(input);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(0, dp(12), 0, 0);
        Button paste = new Button(this);
        paste.setText("Colar");
        paste.setOnClickListener(v -> {
            ClipboardManager cm = getSystemService(ClipboardManager.class);
            ClipData clip = cm == null ? null : cm.getPrimaryClip();
            if (clip != null && clip.getItemCount() > 0 && clip.getItemAt(0).getText() != null) {
                input.setText(clip.getItemAt(0).getText().toString().trim());
            }
        });
        row.addView(paste);
        Button connect = new Button(this);
        connect.setText("Conectar");
        connect.setOnClickListener(v -> connect(input.getText().toString()));
        row.addView(connect);
        col.addView(row);

        status = new TextView(this);
        status.setTextColor(Color.rgb(255, 160, 150));
        status.setPadding(0, dp(16), 0, 0);
        col.addView(status);
        setContentView(col);
    }

    static final String KRONUS_APK = "https://github.com/DragonClaw121/byte-agent/releases/download/app-kronus/kronus.apk";

    private void connect(String raw) {
        String[] parts = Rede.parse(raw);
        if (parts == null) {
            String text = raw == null ? "" : raw;
            if (text.contains("/voz#parear=")) {                // link do /parear: é do app Kronus (o do dono)
                status.setText("Esse link é do app KRONUS (o do dono do agente), não deste. O Meu Assistente é para convidados "
                        + "e usa o link do /painel (com /u/ no meio).\n\nSe você é o dono: abra o app Kronus → aba ⚙️ Ajustes → "
                        + "cole este mesmo link → Conectar. Não tem o Kronus instalado? Toque em \"Baixar o Kronus\".");
                offerKronus();
            } else if (text.contains("/pc/")) {
                status.setText("Esse link é do Bit no PC (Windows): cole-o no BitPC.exe, no computador.");
            } else {
                status.setText("Isso não parece o link do painel (precisa ter https://, /u/ e um # com o código).");
            }
            return;
        }
        status.setTextColor(Color.rgb(170, 178, 196));
        status.setText("Conectando…");
        new Thread(() -> {
            String error;
            try {
                JSONObject me = Rede.get(parts[0] + "/api/eu", parts[1]);
                if (me.optInt("_status") == 200 && me.has("nome")) {
                    Rede.save(this, parts[0], parts[1], me.optString("agente", ""), me.optString("nome", ""));
                    runOnUiThread(() -> {
                        AvisosService.start(this);
                        showPanel();
                    });
                    return;
                }
                error = me.optString("error", "O servidor recusou o link (código " + me.optInt("_status") + ").");
            } catch (Exception e) {
                error = "Sem conexão com o servidor agora. Confira a internet e tente de novo.";
            }
            final String msg = error;
            runOnUiThread(() -> {
                status.setTextColor(Color.rgb(255, 160, 150));
                status.setText(msg);
            });
        }).start();
    }

    private Button kronusButton;

    private void offerKronus() {
        if (kronusButton != null) return;
        kronusButton = new Button(this);
        kronusButton.setText("Baixar o Kronus");
        kronusButton.setOnClickListener(v -> {
            try {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(KRONUS_APK)));
            } catch (Exception ignored) {
            }
        });
        ((LinearLayout) status.getParent()).addView(kronusButton);
    }

    // ------------------------------------------------------------------ o painel pessoal
    private void showPanel() {
        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(16), dp(6), dp(4), dp(6));
        bar.setBackgroundColor(Color.rgb(18, 21, 28));
        TextView name = new TextView(this);
        String agent = Rede.prefs(this).getString("agente", "");
        name.setText(agent.isEmpty() ? "Meu Assistente" : agent);
        name.setTextColor(Color.WHITE);
        name.setTextSize(16);
        name.setTypeface(Typeface.DEFAULT_BOLD);
        bar.addView(name, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button menu = new Button(this);
        menu.setText("⋯");
        menu.setOnClickListener(v -> showMenu());
        bar.addView(menu);
        col.addView(bar);

        web = new WebView(this);
        WebSettings ws = web.getSettings();
        ws.setJavaScriptEnabled(true);
        ws.setDomStorageEnabled(true);
        ws.setAllowFileAccess(false);
        ws.setAllowContentAccess(false);
        final String host = Uri.parse(Rede.base(this)).getHost();
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri u = request.getUrl();
                if (host != null && host.equals(u.getHost())) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));   // links de fora abrem no navegador
                } catch (Exception ignored) {
                }
                return true;
            }
        });
        col.addView(web, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));
        setContentView(col);
        web.loadUrl(Rede.base(this) + "#" + Rede.token(this));
        AvisosService.start(this);
    }

    private void showMenu() {
        boolean avisos = Rede.prefs(this).getBoolean("avisos", true);
        String[] items = {
                avisos ? "🔕 Desligar as notificações" : "🔔 Ligar as notificações",
                "🔄 Recarregar",
                "🔗 Trocar o link do painel",
        };
        new AlertDialog.Builder(this)
                .setItems(items, (d, which) -> {
                    if (which == 0) {
                        Rede.prefs(this).edit().putBoolean("avisos", !avisos).apply();
                        if (avisos) AvisosService.stop(this);
                        else AvisosService.start(this);
                    } else if (which == 1 && web != null) {
                        web.reload();
                    } else if (which == 2) {
                        AvisosService.stop(this);
                        Rede.forget(this);
                        showSetup(null);
                    }
                })
                .show();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }
}
