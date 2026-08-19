package com.apktool.sharereceiver;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

public final class ShareTextProvider extends ContentProvider {
    private static final String AUTHORITY = "com.apktool.sharereceiver.data";

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public String getType(Uri uri) {
        return "text/plain";
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode)
            throws FileNotFoundException {
        if (!AUTHORITY.equals(uri.getAuthority()) || !"latest".equals(uri.getLastPathSegment())) {
            throw new FileNotFoundException("Unknown share text URI");
        }
        File target = new File(getContext().getFilesDir(), ShareReceiverActivity.SHARE_FILE);
        if (!target.isFile()) {
            throw new FileNotFoundException("No share text has been captured yet");
        }
        return ParcelFileDescriptor.open(target, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        File target = new File(getContext().getFilesDir(), ShareReceiverActivity.SHARE_FILE);
        long capturedAt = getContext().getSharedPreferences("capture", 0)
                .getLong("captured_at", 0L);
        MatrixCursor cursor = new MatrixCursor(new String[]{"available", "bytes", "captured_at"});
        cursor.addRow(new Object[]{target.isFile() ? 1 : 0, target.isFile() ? target.length() : 0, capturedAt});
        return cursor;
    }

    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException(); }
    @Override public int delete(Uri uri, String selection, String[] selectionArgs) { return 0; }
    @Override public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) { return 0; }
}
