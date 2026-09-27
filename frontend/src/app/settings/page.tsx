import { redirect } from "next/navigation";

// The Preferences page is now Profile; keep old /settings links working.
export default function SettingsRedirect() {
  redirect("/profile");
}
