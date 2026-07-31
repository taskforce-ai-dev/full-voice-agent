/**
 * mosvoldData.ts — seed for the ISOLATED Mosvold PMS instance.
 *
 * Mirrors the shape of backend/src/seed/demoData.ts but seeds the nine Mosvold
 * Boutique Hotels room types instead of the five Treehouse ones. Drop this into
 * the NEW Mosvold instance's checkout at backend/src/seed/mosvoldData.ts and call
 * seedMosvoldData() from the instance bootstrap (see RUNBOOK.md).
 *
 * DO NOT run this against the shared production PMS (yanolja.taskforceai.tech /
 * database `yanolja_pms`) — that instance feeds the LIVE Treehouse Kavya agent.
 * This seed is for a separate database (e.g. `mosvold_pms`) on a separate PM2
 * process and vhost. See RUNBOOK.md.
 *
 * Room-type NAMES are the single source of property identity in this PMS schema
 * (there is no property column). They MUST match Kavya's ROOM_TYPES_BY_PROPERTY
 * byte-for-byte (case-insensitive tolerated, wording is not) or property routing
 * silently breaks. Do not abbreviate or reword.
 *
 * basePrice is intentionally 0 for every type: Mosvold publishes no rates, and
 * Kavya never quotes a figure. book() multiplies basePrice by nights for the PMS
 * folio total; 0 keeps that non-misleading. Set real rates only if the PMS folio
 * genuinely needs them — never surface them through the agent.
 */
import { Guest, Reservation, Room, RoomType, User } from '../models';

export const seedMosvoldData = async () => {
  if ((await User.count()) === 0) {
    await User.create({
      username: 'admin',
      email: 'admin@mosvold.test',
      password: 'admin123',            // CHANGE before any non-demo use
      role: 'admin',
      firstName: 'Mosvold',
      lastName: 'Admin',
      isActive: true
    });
  }

  if ((await RoomType.count()) === 0) {
    await RoomType.bulkCreate([
      // --- Mosvold Villa (Ahangama) ---
      { name: 'Deluxe Double Room',                 code: 'MV-DDR', basePrice: 0, maxOccupancy: 2, isActive: true },
      { name: 'Deluxe Twin Room',                   code: 'MV-DTR', basePrice: 0, maxOccupancy: 2, isActive: true },
      { name: 'Family Suite',                       code: 'MV-FAM', basePrice: 0, maxOccupancy: 4, isActive: true },
      { name: 'Founders Suite',                     code: 'MV-FND', basePrice: 0, maxOccupancy: 2, isActive: true },
      // --- Sundara by Mosvold (Balapitiya) ---
      { name: 'Deluxe Double Room with Garden View', code: 'SU-DDG', basePrice: 0, maxOccupancy: 2, isActive: true },
      { name: 'Deluxe Double Room with Sea View',    code: 'SU-DDS', basePrice: 0, maxOccupancy: 2, isActive: true },
      { name: 'Deluxe Twin Room with Sea View',      code: 'SU-DTS', basePrice: 0, maxOccupancy: 2, isActive: true },
      { name: 'Beach Villa',                         code: 'SU-BV',  basePrice: 0, maxOccupancy: 4, isActive: true },
      { name: 'Family Villa with Pool',              code: 'SU-FVP', basePrice: 0, maxOccupancy: 4, isActive: true },
      // Fallback for any PMS room whose type is not one of the nine (mirrors demoData).
      { name: 'Default Unmapped Room',               code: 'DUR',    basePrice: 0, maxOccupancy: 0, isActive: true }
    ]);
  }

  if ((await Room.count()) === 0) {
    const rt = await RoomType.findAll({ order: [['id', 'ASC']] });
    const byCode: Record<string, number> = {};
    for (const t of rt) byCode[(t as any).code] = (t as any).id;

    // One bookable physical room per type so availability is non-empty at both
    // properties. Prefix each roomNumber with its property for operator clarity;
    // property identity itself is derived from the room TYPE name, not this string.
    await Room.bulkCreate([
      { roomNumber: 'MV Deluxe Double 01',  roomTypeId: byCode['MV-DDR'], floor: 1, status: 'available', housekeepingStatus: 'clean' },
      { roomNumber: 'MV Deluxe Twin 01',    roomTypeId: byCode['MV-DTR'], floor: 1, status: 'available', housekeepingStatus: 'clean' },
      { roomNumber: 'MV Family Suite 01',   roomTypeId: byCode['MV-FAM'], floor: 1, status: 'available', housekeepingStatus: 'clean' },
      { roomNumber: 'MV Founders Suite 01', roomTypeId: byCode['MV-FND'], floor: 2, status: 'available', housekeepingStatus: 'clean' },
      { roomNumber: 'SU Double Garden 01',  roomTypeId: byCode['SU-DDG'], floor: 1, status: 'available', housekeepingStatus: 'clean' },
      { roomNumber: 'SU Double Sea 01',     roomTypeId: byCode['SU-DDS'], floor: 1, status: 'available', housekeepingStatus: 'clean' },
      { roomNumber: 'SU Twin Sea 01',       roomTypeId: byCode['SU-DTS'], floor: 1, status: 'available', housekeepingStatus: 'clean' },
      { roomNumber: 'SU Beach Villa 01',    roomTypeId: byCode['SU-BV'],  floor: 1, status: 'available', housekeepingStatus: 'clean' },
      { roomNumber: 'SU Family Villa 01',   roomTypeId: byCode['SU-FVP'], floor: 1, status: 'available', housekeepingStatus: 'clean' }
    ]);
  }

  if ((await Guest.count()) === 0) {
    await Guest.bulkCreate([
      { title: 'Ms.', firstName: 'Demo', lastName: 'Guest', mobile: '+94 77 000 0001', email: 'demo1@example.com', country: 'Sri Lanka', isVIP: false },
      { title: 'Mr.', firstName: 'Test', lastName: 'Caller', mobile: '+94 77 000 0002', email: 'demo2@example.com', country: 'United Kingdom', isVIP: false }
    ]);
  }
};

export default seedMosvoldData;
