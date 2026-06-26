export default function Header() {
  return (
    <header className="flex items-center justify-between">
      <div className="flex items-center gap-3">
        <div className="h-10 w-10 rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 text-white flex items-center justify-center font-semibold shadow-sm">
          F
        </div>
        <div>
          <h1 className="text-xl font-semibold leading-tight">
            Flico Customer Service Agent
          </h1>
          <p className="text-xs text-neutral-500">
            Live call logs · Fiona
          </p>
        </div>
      </div>
    </header>
  );
}
