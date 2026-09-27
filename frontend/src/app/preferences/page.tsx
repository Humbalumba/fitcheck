import { redirect } from "next/navigation";

// Old name for the Profile page; keep /preferences links working.
export default function PreferencesRedirect() {
  redirect("/profile");
}
