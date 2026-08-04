import { redirect } from "next/navigation";
import { auth } from "../../auth";
import Sandbox from "../../components/Sandbox";

export default async function SandboxPage() {
  const session = await auth();
  if (!session) redirect("/signin");

  return <Sandbox />;
}
