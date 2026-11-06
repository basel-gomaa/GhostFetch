import { useRef, useState } from "react";
import { api, User } from "./api";
import { IconCamera } from "./brand/icons";
import { Avatar, Banner, EmptyState, ErrorBanner, PageHeader, displayName, fmtTime, useLoad } from "./ui";

const AVATAR_SIDE = 256;
const MAX_SOURCE_BYTES = 20 * 1024 * 1024;

/** Any image the browser can open -> a 256x256, centre-cropped WebP (or JPEG
 *  where WebP encoding is unavailable). Re-encoding through a canvas also drops
 *  the original file's metadata -- a phone photo's GPS position included -- so
 *  only pixels ever leave this page. */
async function toAvatar(file: File): Promise<Blob> {
  if (file.size > MAX_SOURCE_BYTES) throw new Error("That file is over 20 MB. Pick a smaller photo.");
  let bitmap: ImageBitmap;
  try { bitmap = await createImageBitmap(file); }
  catch { throw new Error("That file is not an image this browser can open. Try a JPEG or PNG."); }
  const side = Math.min(bitmap.width, bitmap.height);
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = AVATAR_SIDE;
  const ctx = canvas.getContext("2d");
  if (!ctx) throw new Error("This browser cannot resize the photo.");
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bitmap, (bitmap.width - side) / 2, (bitmap.height - side) / 2, side, side,
                0, 0, AVATAR_SIDE, AVATAR_SIDE);
  bitmap.close();
  const encode = (type: string, quality: number) =>
    new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, type, quality));
  const webp = await encode("image/webp", 0.88);
  // A browser that cannot encode WebP silently hands back a PNG instead.
  const blob = webp && webp.type === "image/webp" ? webp : await encode("image/jpeg", 0.9);
  if (!blob) throw new Error("The photo could not be prepared.");
  return blob;
}

/** Name and photo: how this person appears in GhostFetch. Neither changes how
 *  they sign in -- that is the username, shown here so the difference is plain. */
function Profile({ user, onUser }: { user: User; onUser: (u: User) => void }) {
  const [name, setName] = useState(user.display_name ?? "");
  const [busy, setBusy] = useState<"name" | "photo" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const dirty = name.trim() !== (user.display_name ?? "");

  const done = (u: User, message: string) => { onUser(u); setSaved(message); setError(null); };

  const saveName = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy("name"); setSaved(null);
    try {
      const u = await api.updateProfile(name.trim() || null);
      setName(u.display_name ?? "");
      done(u, u.display_name ? `You now appear as ${u.display_name}.` : "Display name cleared; your username is shown instead.");
    } catch (err) { setError(err); } finally { setBusy(null); }
  };

  const pickPhoto = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files?.[0];
    e.target.value = "";            // picking the same file again still fires
    if (!picked) return;
    setBusy("photo"); setSaved(null);
    try {
      done(await api.uploadAvatar(await toAvatar(picked)), "Photo updated.");
    } catch (err) {
      setError(err instanceof Error && !("status" in err) ? { error: err.message } : err);
    } finally { setBusy(null); }
  };

  const removePhoto = async () => {
    setBusy("photo"); setSaved(null);
    try { done(await api.deleteAvatar(), "Photo removed."); }
    catch (err) { setError(err); } finally { setBusy(null); }
  };

  return (
    <div className="card profile">
      <div className="profile-photo">
        <button type="button" className="avatar-button" onClick={() => file.current?.click()}
                disabled={busy !== null} title="Change your photo" aria-label="Change your photo">
          <Avatar user={user} size={96} />
          <span className="avatar-overlay" aria-hidden>
            {busy === "photo" ? <span className="spin" /> : <IconCamera size={22} />}
          </span>
        </button>
        <input ref={file} type="file" accept="image/*" hidden onChange={pickPhoto} />
        <div className="row" style={{ gap: 12, justifyContent: "center" }}>
          <button type="button" className="link" disabled={busy !== null}
                  onClick={() => file.current?.click()}>
            {user.avatar_version ? "Change photo" : "Add a photo"}
          </button>
          {user.avatar_version ? (
            <button type="button" className="link danger" disabled={busy !== null} onClick={removePhoto}>
              Remove
            </button>
          ) : null}
        </div>
      </div>

      <form className="stack profile-fields" onSubmit={saveName}>
        <div>
          <h3 style={{ margin: 0 }}>{displayName(user)}</h3>
          <div className="muted">@{user.username} · {user.email}</div>
        </div>
        <div>
          <label htmlFor="display_name">Display name</label>
          <div className="row" style={{ flexWrap: "nowrap" }}>
            <input id="display_name" maxLength={50} autoComplete="name" placeholder={user.username}
                   value={name} onChange={(e) => { setName(e.target.value); setSaved(null); }} />
            <button className="primary" type="submit" disabled={busy !== null || !dirty}>
              {busy === "name" ? <span className="spin" /> : null}Save
            </button>
          </div>
          <div className="hint">
            What GhostFetch calls you — anything you like, up to 50 characters. You still
            sign in as <strong>{user.username}</strong>; that does not change. Leave it empty to
            show your username.
          </div>
        </div>
        {saved ? <Banner kind="ok">{saved}</Banner> : null}
        <ErrorBanner error={error} />
        <div className="hint" style={{ marginTop: 0 }}>
          Photos are cropped to a square and resized in your browser before upload, which also
          strips the original file's details (such as where it was taken).
        </div>
      </form>
    </div>
  );
}

export function AccountPage({ user, onUser }: { user: User; onUser: (u: User) => void }) {
  const keys = useLoad<any[]>(() => api.apiKeys(), []);
  const [fresh, setFresh] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [error, setError] = useState<unknown>(null);

  const create = async () => {
    setError(null);
    try {
      const made = await api.createApiKey(label || "console");
      setFresh(made.key);
      setLabel("");
      keys.reload();
    } catch (e) { setError(e); }
  };

  return (
    <div className="stack">
      <PageHeader title="Profile & account" sub={<>
          Signed in as <strong>{user.username}</strong> ({user.email})
          {user.is_admin ? <> — <strong>administrator</strong></> : null}. Reports are
          e-mailed to this address.
        </>} />

      <Profile user={user} onUser={onUser} />

      <ErrorBanner error={error} />

      <h2>API keys</h2>
      <p className="muted" style={{ marginTop: -6 }}>
        For driving the platform from a script. A key is shown <strong>once</strong>:
        only a hash of it is stored, so it cannot be recovered — by us either.
      </p>

      {fresh ? (
        <Banner kind="ok">
          <div>Copy this now — it will not be shown again.</div>
          <code style={{ userSelect: "all", wordBreak: "break-all" }}>{fresh}</code>
        </Banner>
      ) : null}

      <div className="row">
        <input className="grow" style={{ maxWidth: 280 }} placeholder="Label (e.g. laptop)"
               value={label} onChange={(e) => setLabel(e.target.value)} />
        <button className="primary" onClick={create}>Create key</button>
      </div>

      {keys.data?.length ? (
        <table>
          <thead><tr><th>Label</th><th>Key id</th><th>Scopes</th><th>Created</th><th>Last used</th><th /></tr></thead>
          <tbody>
            {keys.data.map((k) => (
              <tr key={k.id}>
                <td>{k.label ?? "—"}</td>
                <td><code>{k.key_id}</code></td>
                <td>{(k.scopes ?? []).join(", ")}</td>
                <td>{fmtTime(k.created_at)}</td>
                <td>{k.last_used_at ? fmtTime(k.last_used_at) : <span className="muted">never</span>}</td>
                <td>
                  <button className="danger link"
                          onClick={() => api.revokeApiKey(k.id).then(keys.reload).catch(setError)}>
                    Revoke
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <EmptyState compact title="No API keys yet">
          Create one above to drive GhostFetch from a script.
        </EmptyState>
      )}
    </div>
  );
}
