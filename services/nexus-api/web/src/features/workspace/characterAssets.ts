export const employeeCharacters = Array.from({ length: 9 }, (_, id) => ({
  id,
  name: `形象 ${id + 1}`,
}))

export function employeeCharacterSource(characterId: number): string {
  return `/assets/office/characters/character-${characterId}-front.png`
}

export function stableEmployeeNumber(id: string): number {
  return [...id].reduce((total, character) => ((total * 31) + character.charCodeAt(0)) >>> 0, 7)
}
