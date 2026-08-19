package com.apktool.sharereceiver;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.widget.Toast;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;

public final class ShareReceiverActivity extends Activity {
    static final String SHARE_FILE = "latest_share.txt";

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        capture(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        capture(intent);
    }

    private void capture(Intent intent) {
        CharSequence shared = null;
        if (intent != null && Intent.ACTION_SEND.equals(intent.getAction())) {
            shared = intent.getCharSequenceExtra(Intent.EXTRA_TEXT);
        }
        if (shared == null || shared.length() == 0) {
            Toast.makeText(this, "没有收到可用的分享文本", Toast.LENGTH_SHORT).show();
            finish();
            return;
        }
        try {
            File target = new File(getFilesDir(), SHARE_FILE);
            try (FileOutputStream output = new FileOutputStream(target, false)) {
                output.write(shared.toString().getBytes(StandardCharsets.UTF_8));
                output.flush();
            }
            getSharedPreferences("capture", MODE_PRIVATE)
                    .edit()
                    .putLong("captured_at", System.currentTimeMillis())
                    .apply();
            Toast.makeText(this, "MAX 调试文本已保存", Toast.LENGTH_SHORT).show();
        } catch (Exception error) {
            Toast.makeText(this, "保存分享文本失败", Toast.LENGTH_LONG).show();
        }
        finish();
    }
}
