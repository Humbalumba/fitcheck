import { googleEnabled } from "@/auth";
import { SignupForm } from "./SignupForm";

export const dynamic = "force-dynamic"; // Google button depends on runtime env
export const metadata = { title: "Create account · FitCheck" };

export default function SignupPage() {
  return <SignupForm google={googleEnabled} />;
}
