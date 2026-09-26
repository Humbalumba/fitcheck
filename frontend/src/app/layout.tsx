import type { Metadata, Viewport } from "next";
import "./globals.css";
import { AppShell } from "@/components/AppShell";
import { DOORS_SEEN_KEY } from "@/lib/constants";

export const metadata: Metadata = {
  title: "FitCheck — should I buy it?",
  description: "Find out how many new outfits a piece creates with your closet before you buy it.",
  appleWebApp: { capable: true, title: "FitCheck", statusBarStyle: "default" },
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  themeColor: "#ede5da",
};

// Runs before first paint: skip the closet-door intro if it already played this session, the user prefers reduced
// motion, or the first page isn't the closet. (Adds a class on <html>, which is suppressHydrationWarning.)
const doorsScript = `(function(){var d=document.documentElement;try{if(sessionStorage.getItem(${JSON.stringify(
  DOORS_SEEN_KEY,
)})||window.matchMedia("(prefers-reduced-motion: reduce)").matches||!/^\\/(closet\\/?)?$/.test(location.pathname)){d.classList.add("fc-no-doors")}}catch(e){d.classList.add("fc-no-doors")}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full antialiased" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: doorsScript }} />
      </head>
      <body className="min-h-full" suppressHydrationWarning>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
