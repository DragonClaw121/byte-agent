package io.byteagent.assistente;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Depois de ligar o celular (ou atualizar o app), volta a ficar atento aos avisos. */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        AvisosService.start(context);
    }
}
