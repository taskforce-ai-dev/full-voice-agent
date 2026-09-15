import FactoryConsole from "@/components/factory/FactoryConsole";

export const metadata = {
  title: "Factory Console · Flico",
  description: "Owner-only SmartPBX agent factory review console",
  robots: { index: false, follow: false },
};

export default function FactoryPage() {
  return <FactoryConsole />;
}
