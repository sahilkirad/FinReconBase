import { FinOpsView } from "@/components/finops/finops-view";

export default async function FinancialOpsPage({
  params,
}: {
  params: Promise<{ batchId: string }>;
}) {
  const { batchId } = await params;
  return <FinOpsView batchId={batchId} />;
}
