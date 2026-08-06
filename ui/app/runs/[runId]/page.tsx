import { TraceScreen } from "@/components/traces/TraceScreen";

export default async function RunPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  return <TraceScreen runId={runId} />;
}
