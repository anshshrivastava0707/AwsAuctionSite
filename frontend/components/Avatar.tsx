import { imageUrl } from "@/lib/api";

export default function Avatar({ name, avatarKey, size = 32 }: { name: string; avatarKey?: string | null; size?: number }) {
  const style = { width: size, height: size, fontSize: size * 0.42 };
  if (avatarKey) {
    // eslint-disable-next-line @next/next/no-img-element -- images are served by our API, not next/image
    return <img className="avatar" src={imageUrl(avatarKey)} alt="" style={style} />;
  }
  return <span className="avatar" style={style} aria-hidden>{(name.trim()[0] ?? "?").toUpperCase()}</span>;
}
